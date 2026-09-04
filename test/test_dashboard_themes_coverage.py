"""Coverage tests for ``kiro_crew.dashboard.handlers.themes`` — HTTP surface.

``test_theme_install.py`` covers the pure validation core plus a few module
helpers; this file covers the parts a validator test never reaches: the six
aiohttp handlers (list / create / install / detail / asset / overlay / topbar),
the blocking workers they offload to the discovery pool (``_list_themes_sync``,
``_do_install``), the local/GitHub source resolvers, and the refusal branches —
invalid JSON, slug traversal, governance denial, read-only installed packs,
unsupported asset types, and cross-platform pack install/serving.

Every test points ``KIROCREW_HOME`` at ``tmp_path`` so ``_themes_dir()``
resolves inside the sandbox: nothing is written outside it. No network, no git,
no real subprocess — ``_clone_github``'s spawn is replaced with a stub so only
its URL guard and error mapping are exercised.

Platform notes: install and serving exercise the real descriptor-containment
chokepoint on every supported OS. Tests that need to create symbolic links run
where the process has that capability, including privileged Windows CI runners.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import kiro_crew.platform.governance_profiles as gov_mod
from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import themes as th

# _validate_theme_data only *requires* --bg/--text/--accent per mode.
_VALID_VARS: dict[str, dict[str, str]] = {
    "dark": {"--bg": "#000000", "--text": "#ffffff", "--accent": "#3366ff"},
    "light": {"--bg": "#ffffff", "--text": "#000000", "--accent": "#0033cc"},
}


# ── helpers ────────────────────────────────────────────────────────────────


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8", newline="\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _body(response: web.Response) -> Any:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _request(
    method: str, path: str, *, body: object = ..., match_info: dict | None = None
) -> web.Request:
    """A real (mocked) aiohttp request.

    ``body=None`` models a malformed payload: ``request.json()`` raising is what
    the handlers' ``except Exception -> 400`` branches are written for.
    """
    req = make_mocked_request(method, path, match_info=match_info or {})
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    elif body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _theme_body(name: str = "Sunset", emoji: str = "🌇") -> dict[str, Any]:
    return {"name": name, "emoji": emoji, **_VALID_VARS}


def _make_pack(root: Path, *, slug: str = "lcars", level: int = 0) -> Path:
    """Build a valid Level-0 pack directory at ``root`` and return it."""
    root.mkdir(parents=True, exist_ok=True)
    _write_json(
        root / "theme.json",
        {
            "slug": slug,
            "name": "LCARS",
            "emoji": "🖖",
            "level": level,
            "formatVersion": 1,
        },
    )
    _write_json(root / "variables.json", _VALID_VARS)
    return root


@pytest.fixture
def themes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect KIROCREW_HOME into tmp_path and return the themes directory."""
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    d = home / "themes"
    d.mkdir()
    assert os.path.realpath(str(th._themes_dir())) == os.path.realpath(str(d))
    return d


@pytest.fixture
def allow_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Governance admits the install (default-allow standalone), deterministically."""

    class _Allowed:
        permitted = True
        rule = "capabilities.theme_install"
        layer = "standalone"
        reason = ""

    monkeypatch.setattr(
        gov_mod, "governance_permits", lambda *a, **k: _Allowed(), raising=True
    )


# ── _list_themes_sync ──────────────────────────────────────────────────────


class TestListThemesSync:
    def test_missing_directory_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "nowhere"))
        assert th._list_themes_sync() == []

    def test_custom_record_defaults_fill_missing_fields(self, themes_dir: Path) -> None:
        _write_json(themes_dir / "sunset.json", {})
        (row,) = th._list_themes_sync()
        assert row == {"slug": "sunset", "name": "sunset", "emoji": "🎨", "created_at": ""}

    def test_unparseable_custom_record_is_skipped(self, themes_dir: Path) -> None:
        _write_text(themes_dir / "broken.json", "{not json")
        _write_json(themes_dir / "ok.json", {"name": "Ok"})
        assert [r["slug"] for r in th._list_themes_sync()] == ["ok"]

    def test_installed_pack_carries_source_and_level(self, themes_dir: Path) -> None:
        _write_json(
            themes_dir / "lcars" / "theme.json",
            {"name": "LCARS", "emoji": "🖖", "level": 2, "created_at": "2026-01-01"},
        )
        (row,) = th._list_themes_sync()
        assert row["source"] == "installed"
        assert row["level"] == 2
        assert row["name"] == "LCARS"

    def test_installed_pack_defaults_when_manifest_is_sparse(self, themes_dir: Path) -> None:
        _write_json(themes_dir / "bare" / "theme.json", {})
        (row,) = th._list_themes_sync()
        assert row["name"] == "bare"
        assert row["emoji"] == th._THEME_DEFAULT_EMOJI
        assert row["level"] == 0

    def test_directory_without_manifest_is_skipped(self, themes_dir: Path) -> None:
        (themes_dir / "not-a-theme").mkdir()
        assert th._list_themes_sync() == []

    def test_directory_with_corrupt_manifest_is_skipped(self, themes_dir: Path) -> None:
        _write_text(themes_dir / "bad" / "theme.json", "{{{")
        assert th._list_themes_sync() == []

    def test_dot_prefixed_staging_and_backup_dirs_are_never_listed(
        self, themes_dir: Path
    ) -> None:
        _write_json(themes_dir / ".install-staging-abc" / "theme.json", {"name": "X"})
        _write_json(themes_dir / ".lcars.old-abc" / "theme.json", {"name": "Y"})
        assert th._list_themes_sync() == []

    @requires_symlinks
    def test_symlinked_directory_is_never_listed(self, themes_dir: Path, tmp_path: Path) -> None:
        real = _make_pack(tmp_path / "outside")
        (themes_dir / "linked").symlink_to(real, target_is_directory=True)
        assert th._list_themes_sync() == []

    def test_sorted_oldest_first_with_undated_last(self, themes_dir: Path) -> None:
        _write_json(themes_dir / "b.json", {"name": "B", "created_at": "2026-05-05"})
        _write_json(themes_dir / "a.json", {"name": "A", "created_at": "2026-01-01"})
        _write_json(themes_dir / "z.json", {"name": "Z"})
        assert [r["slug"] for r in th._list_themes_sync()] == ["a", "b", "z"]


# ── GET /api/themes ────────────────────────────────────────────────────────


class TestApiThemes:
    @pytest.mark.asyncio
    async def test_returns_the_enumerated_list(self, themes_dir: Path) -> None:
        _write_json(themes_dir / "sunset.json", {"name": "Sunset"})
        resp = await th.api_themes(_request("GET", "/api/themes"))
        assert resp.status == 200
        assert [t["slug"] for t in _body(resp)["themes"]] == ["sunset"]


# ── POST /api/themes ───────────────────────────────────────────────────────


class TestApiThemesCreate:
    @pytest.mark.asyncio
    async def test_malformed_json_is_400(self, themes_dir: Path) -> None:
        resp = await th.api_themes_create(_request("POST", "/api/themes", body=None))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_validation_error_is_surfaced(self, themes_dir: Path) -> None:
        resp = await th.api_themes_create(
            _request("POST", "/api/themes", body={"name": "  "})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "name is required"

    @pytest.mark.asyncio
    async def test_writes_the_record_and_returns_it(self, themes_dir: Path) -> None:
        resp = await th.api_themes_create(
            _request("POST", "/api/themes", body=_theme_body("Sun Set"))
        )
        assert resp.status == 200
        payload = _body(resp)
        assert payload["slug"] == "sun-set"
        on_disk = json.loads((themes_dir / "sun-set.json").read_text("utf-8"))
        assert on_disk["name"] == "Sun Set"
        assert on_disk["dark"]["--bg"] == "#000000"
        assert on_disk["created_at"]

    @pytest.mark.asyncio
    async def test_blank_emoji_falls_back_to_the_default(self, themes_dir: Path) -> None:
        resp = await th.api_themes_create(
            _request("POST", "/api/themes", body=_theme_body(emoji="   "))
        )
        assert _body(resp)["theme"]["emoji"] == th._THEME_DEFAULT_EMOJI

    @pytest.mark.asyncio
    async def test_long_emoji_is_truncated(self, themes_dir: Path) -> None:
        resp = await th.api_themes_create(
            _request("POST", "/api/themes", body=_theme_body(emoji="abcdefgh"))
        )
        assert _body(resp)["theme"]["emoji"] == "abcd"

    @pytest.mark.asyncio
    async def test_existing_record_is_409(self, themes_dir: Path) -> None:
        _write_json(themes_dir / "sunset.json", {"name": "Sunset"})
        resp = await th.api_themes_create(
            _request("POST", "/api/themes", body=_theme_body())
        )
        assert resp.status == 409
        assert "already exists" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_installed_pack_with_the_same_slug_is_409(self, themes_dir: Path) -> None:
        # The in-lock check refuses a slug already taken by an installed
        # <slug>/ directory, not just an existing <slug>.json record.
        (themes_dir / "sunset").mkdir()
        resp = await th.api_themes_create(
            _request("POST", "/api/themes", body=_theme_body())
        )
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_creates_the_themes_directory_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "fresh"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        resp = await th.api_themes_create(
            _request("POST", "/api/themes", body=_theme_body())
        )
        assert resp.status == 200
        assert (home / "themes" / "sunset.json").is_file()


class TestApiThemesCreateOffLoop:
    """#6198: the create handler's filesystem work must never run on the loop.

    On a UNC data home ``mkdir``/``exists`` are SMB-backed and can block for
    as long as the network takes; one such call on the loop stalls every other
    request the gateway serves. Spy on ``Path.mkdir``/``exists``/``is_dir``/
    ``is_file`` for the handler's paths (and the data home itself) and assert
    every call happened on a worker thread — the same discipline #5963 pinned
    for the detail route's target stats.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("preexisting", [False, True])
    async def test_filesystem_work_runs_on_a_worker_thread(
        self, preexisting: bool, themes_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if preexisting:
            _write_json(themes_dir / "sunset.json", {"name": "Sunset"})
        # Build the watched set through the handler's own resolver: the
        # fixture path is unresolved, while ``_themes_dir()`` goes through
        # ``config_dir()``'s ``resolve()`` — on Darwin ``/tmp`` is a symlink,
        # so comparing against the raw fixture path would never match. The
        # data home itself is watched too, so a cold ``config_dir()``
        # resolve sneaking onto the loop would also be caught.
        watched = {
            th._themes_dir().parent,
            th._themes_dir(),
            th._themes_dir() / "sunset.json",
            th._themes_dir() / "sunset",
        }
        loop_thread = threading.get_ident()
        fs_threads: list[int] = []
        real_calls = {
            name: getattr(Path, name) for name in ("mkdir", "exists", "is_dir", "is_file")
        }

        def _spy(name: str) -> Any:
            real = real_calls[name]

            def spy(self: Path, *args: Any, **kwargs: Any) -> Any:
                if self in watched:
                    fs_threads.append(threading.get_ident())
                return real(self, *args, **kwargs)

            return spy

        for name in real_calls:
            monkeypatch.setattr(Path, name, _spy(name))

        resp = await th.api_themes_create(
            _request("POST", "/api/themes", body=_theme_body())
        )
        assert resp.status == (409 if preexisting else 200)
        assert fs_threads, "expected the handler to touch the filesystem"
        on_loop = [t for t in fs_threads if t == loop_thread]
        assert not on_loop, f"{len(on_loop)} filesystem call(s) ran on the event loop"


# ── _resolve_local_source ──────────────────────────────────────────────────


class TestResolveLocalSource:
    @pytest.mark.parametrize("bad", ["", "   ", None, 7])
    def test_missing_path_is_rejected(self, bad: object) -> None:
        src, err = th._resolve_local_source(bad)  # type: ignore[arg-type]
        assert src is None
        assert err == "local 'path' is required"

    def test_non_directory_is_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        _write_text(f, "x")
        src, err = th._resolve_local_source(str(f))
        assert src is None
        assert err is not None and "not a directory" in err

    @requires_symlinks
    def test_symlinked_source_is_rejected(self, tmp_path: Path) -> None:
        real = _make_pack(tmp_path / "real")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        src, err = th._resolve_local_source(str(link))
        assert src is None
        assert err == "local path must not be a symlink"

    def test_sensitive_location_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = _make_pack(tmp_path / "creds")
        monkeypatch.setattr(th, "is_sensitive_path", lambda _p: True)
        src, err = th._resolve_local_source(str(d))
        assert src is None
        assert err == "local path is not an allowed location"

    def test_existing_directory_resolves(self, tmp_path: Path) -> None:
        d = _make_pack(tmp_path / "pack")
        src, err = th._resolve_local_source(str(d))
        assert err is None
        assert src is not None
        # realpath BOTH sides: Windows temp dirs hand back the 8.3 short form.
        assert os.path.realpath(str(src)) == os.path.realpath(str(d))

    def test_a_unc_source_is_refused_without_touching_the_filesystem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A UNC path names a HOST, so stat-ing one is the vulnerability itself.

        `\\\\attacker\\share` reaching `is_dir()` makes Windows open an SMB
        connection and authenticate, handing this gateway's credentials to a host
        the caller chose. So the assertion that matters is not merely that the
        path is refused -- it is that **no filesystem call happens at all**. Any
        stat is wired to fail the test.
        """
        monkeypatch.setattr(th, "IS_WINDOWS", True)
        monkeypatch.setattr(th, "unc_probe_allowed", lambda _p: False)

        def _no_fs(*_a: object, **_k: object) -> bool:
            raise AssertionError("touched the filesystem on a UNC path")

        monkeypatch.setattr(th.Path, "is_dir", _no_fs)
        monkeypatch.setattr(th, "is_link_or_junction", _no_fs)

        src, err = th._resolve_local_source(r"\\attacker\share\pack")
        assert src is None
        assert err == "local path is not an allowed location"

    def test_a_roaming_profile_unc_source_gets_past_the_shape_screen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The screen must not blanket-ban UNC.

        On a roaming profile the home directory is itself a UNC share, which is the
        one legitimate source of a UNC pack path -- telling that apart from
        `\\\\attacker\\share` is `unc_probe_allowed`'s whole job. So a permitted UNC
        path must reach the ordinary checks instead of being refused for its shape.
        This is the exact inverse of the refusal test above: there `is_dir` must
        never be called, here it must be.
        """
        monkeypatch.setattr(th, "IS_WINDOWS", True)
        monkeypatch.setattr(th, "unc_probe_allowed", lambda _p: True)
        monkeypatch.setattr(th, "is_link_or_junction", lambda _p: False)
        reached: list[str] = []

        def _reached(self: object) -> bool:
            reached.append(str(self))
            return False  # stop here; the branch under test is the screen above

        monkeypatch.setattr(th.Path, "is_dir", _reached)

        src, err = th._resolve_local_source(r"\\roaming\profile\pack")
        assert reached, "a probe-allowed UNC path must get past the shape screen"
        assert src is None
        assert err is not None and "not a directory" in err

    def test_a_junction_root_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`Path.is_symlink()` is False for a Windows junction, so a junction as
        the pack ROOT passed an islink-only guard and was resolved through. The
        root must use the same junction-aware predicate as the copy walk."""
        d = _make_pack(tmp_path / "pack")
        monkeypatch.setattr(th, "is_link_or_junction", lambda _p: True)
        src, err = th._resolve_local_source(str(d))
        assert src is None
        assert err == "local path must not be a symlink"


# ── _clone_github ──────────────────────────────────────────────────────────


class TestCloneGithubGuard:
    """The URL guard runs before any spawn; the spawn itself is stubbed."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://user@github.com/o/r",
            "https://user:pw@github.com/o/r",
            "https://github.com/o/r?token=1",
            "https://github.com/o/r#frag",
        ],
    )
    def test_decorated_urls_are_rejected(self, url: str, tmp_path: Path) -> None:
        err = th._clone_github(url, tmp_path / "clone")
        assert err == "github URL must not contain credentials, query, or fragment"

    @pytest.mark.parametrize("url", [None, 42, ""])
    def test_missing_url_is_rejected(self, url: object, tmp_path: Path) -> None:
        assert th._clone_github(url, tmp_path / "clone") == "github 'url' is required"  # type: ignore[arg-type]

    @pytest.fixture(autouse=True)
    def _no_real_sandbox(self, monkeypatch: pytest.MonkeyPatch):
        """Keep every test in this class off the real sandbox chokepoint.

        `_clone_github` routes its argv through `sandboxed_spawn_argv` BEFORE it
        calls subprocess.run, and that raises SandboxUnavailableError on any host
        without an OS sandbox backend -- which is every GitHub Actions runner. So
        stubbing subprocess.run alone passes on a dev desk (namespace backend
        present) and fails in CI before reaching the branch under test.

        The third element is a scratch PATH (or falsy), not a callable -- the
        product does `Path(cleanup).unlink(missing_ok=True)`. `None` means "no
        scratch file to remove". Being autouse, this lands before the test body,
        so a test that needs a real scratch path just re-stubs it and wins.
        """
        monkeypatch.setattr(
            th, "sandboxed_spawn_argv", lambda argv, *a, **k: (list(argv), {}, None)
        )

    def _stub_run(self, monkeypatch: pytest.MonkeyPatch, outcome: object) -> list[list[str]]:
        seen: list[list[str]] = []

        def _run(argv: list[str], **kwargs: object) -> object:
            seen.append(argv)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(th.subprocess, "run", _run)
        return seen

    def test_missing_git_binary_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_run(monkeypatch, FileNotFoundError("git"))
        err = th._clone_github("https://github.com/o/r", tmp_path / "clone")
        assert err == "git is not available on the server"

    def test_sandbox_unavailable_is_reported_without_spawning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _unavailable(*args: object, **kwargs: object) -> object:
            raise th.SandboxUnavailableError(
                "no backend", kind="no_backend", detail="unsupported host"
            )

        monkeypatch.setattr(th, "sandboxed_spawn_argv", _unavailable)
        seen = self._stub_run(monkeypatch, AssertionError("must not spawn"))

        err = th._clone_github("https://github.com/o/r", tmp_path / "clone")

        assert err == th._THEME_GIT_SANDBOX_UNAVAILABLE
        assert seen == []

    def test_timeout_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_run(
            monkeypatch, th.subprocess.TimeoutExpired(cmd="git", timeout=1.0)
        )
        assert th._clone_github("https://github.com/o/r", tmp_path / "c") == (
            "git clone timed out"
        )

    def test_nonzero_exit_reports_redacted_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Proc:
            returncode = 128
            stderr = "fatal: repository not found\n"

        self._stub_run(monkeypatch, _Proc())
        err = th._clone_github("https://www.github.com/o/r", tmp_path / "c")
        assert err is not None and err.startswith("git clone failed:")
        assert "repository not found" in err

    def test_success_returns_none_and_passes_the_url_as_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Proc:
            returncode = 0
            stderr = ""

        seen = self._stub_run(monkeypatch, _Proc())
        assert th._clone_github("https://github.com/o/r", tmp_path / "c") is None
        # argv form (never a shell string), and the URL is a discrete token.
        assert "https://github.com/o/r" in seen[0]

    def test_sandbox_scratch_file_is_always_unlinked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The sandbox wrapper can hand back a scratch profile path; the finally
        # block owns removing it whether or not the clone succeeded.
        scratch = tmp_path / "sandbox-profile"
        _write_text(scratch, "profile\n")
        monkeypatch.setattr(
            th,
            "sandboxed_spawn_argv",
            lambda argv, *a, **k: (list(argv), {}, str(scratch)),
        )
        self._stub_run(monkeypatch, FileNotFoundError("git"))
        assert th._clone_github("https://github.com/o/r", tmp_path / "c") is not None
        assert not scratch.exists()


# ── _copy_installed_theme swap-race guards ─────────────────────────────────


class TestCopyInstalledThemeSwapGuards:
    """Both bounds are written against a source that stays writable, so they are
    reached by simulating what a swapped file returns — not by racing one."""

    def test_unreadable_file_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = _make_pack(tmp_path / "pack")
        monkeypatch.setattr(th, "safe_read_file_bytes_nolink", lambda *a, **k: None)
        with pytest.raises(ValueError, match="unreadable/unsafe file"):
            th._copy_installed_theme(src, tmp_path / "dst")

    def test_post_read_byte_ceiling_is_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = _make_pack(tmp_path / "pack")
        budget = max(th._THEME_TOTAL_BYTES_BY_LEVEL.values())
        # A small file on disk (so the pre-read lstat bound passes) whose READ
        # returns more bytes than the ceiling — the regular-file swap case.
        monkeypatch.setattr(
            th, "safe_read_file_bytes_nolink", lambda *a, **k: b"x" * (budget + 1)
        )
        with pytest.raises(ValueError, match="maximum install size"):
            th._copy_installed_theme(src, tmp_path / "dst")

    def test_a_junction_subdirectory_is_refused_like_a_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Windows junction is not a symlink, and that distinction is the bug.

        ``os.path.islink`` returns False for a junction and ``os.walk`` reports one
        as an ordinary directory, so a guard that only checked ``islink`` would
        DESCEND a junction. A pack carrying a junction back to its own root then
        recurses until a path-length ``OSError`` escapes the handler as a 500 —
        reachable the moment local install is enabled on Windows. The guard must
        therefore consult the junction-aware predicate, not ``islink``.

        Patched on ``th`` rather than on ``platform_compat`` because the module
        binds the name with ``from ... import``, so that namespace is the one the
        call actually resolves through.
        """
        src = _make_pack(tmp_path / "pack")
        (src / "nested").mkdir()
        monkeypatch.setattr(
            th,
            "is_link_or_junction",
            lambda p: os.path.basename(str(p)) == "nested",
        )
        with pytest.raises(ValueError, match="refusing to install symlinked directory"):
            th._copy_installed_theme(src, tmp_path / "dst")


# ── _atomic_write_theme_json ───────────────────────────────────────────────


class TestAtomicWriteFailureCleanup:
    def test_a_failed_temp_unlink_still_reraises_the_original_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Accepts what os.unlink really takes (dir_fd): this replaces the os module
        # attribute, so it is live through teardown, where pytest's tmp_path cleanup
        # calls unlink with dir_fd=.
        def _no_unlink(_path: object, **_kwargs: object) -> None:
            raise OSError("temp already gone")

        monkeypatch.setattr(th.os, "unlink", _no_unlink)
        # The non-str payload makes the write raise; the cleanup failure must not
        # mask it, and the target must never appear.
        with pytest.raises(TypeError):
            th._atomic_write_theme_json(tmp_path / "sunset.json", 123)  # type: ignore[arg-type]
        assert not (tmp_path / "sunset.json").exists()


# ── _do_install failure cleanup ────────────────────────────────────────────


class TestDoInstallFailureCleanup:
    """The staging snapshot must not survive an unexpected failure, and a failed
    promotion must roll the previous pack back. Both are simulated at the
    module's own seams so they run on every platform."""

    def test_unexpected_error_clears_the_staging_snapshot(
        self, themes_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = _make_pack(tmp_path / "pack")

        def _boom(_src: Path, _dst: Path) -> None:
            raise OSError("disk went away")

        monkeypatch.setattr(th, "_copy_installed_theme", _boom)
        with pytest.raises(OSError):
            th._do_install("local", {"path": str(src)})
        assert list(themes_dir.glob(".install-staging-*")) == []

    def test_failed_promotion_rolls_the_previous_pack_back(
        self, themes_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = _make_pack(tmp_path / "pack")
        installed = _make_pack(themes_dir / "lcars")
        _write_text(installed / "readme.md", "previous revision\n")

        def _stage(_src: Path, dst: Path) -> None:
            _make_pack(dst)

        monkeypatch.setattr(th, "_copy_installed_theme", _stage)
        monkeypatch.setattr(
            th,
            "_validate_theme_dir",
            lambda path, **k: (
                {
                    "slug": "lcars",
                    "name": "LCARS",
                    "emoji": "🖖",
                    "level": 0,
                    "dark": {},
                    "light": {},
                },
                None,
            ),
        )
        real_replace = Path.replace

        def _replace(self: Path, target: object) -> Path:
            if self.name.startswith(".install-staging-"):
                raise OSError("cross-device rename")
            return real_replace(self, target)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "replace", _replace)
        with pytest.raises(OSError):
            th._do_install("local", {"path": str(src)})
        # The previous pack is back where it was, and nothing is left staged.
        assert (themes_dir / "lcars" / "readme.md").read_text("utf-8").strip() == (
            "previous revision"
        )
        assert list(themes_dir.glob(".install-staging-*")) == []
        assert list(themes_dir.glob(".lcars.old-*")) == []


# ── _read_theme_bytes_nolink ───────────────────────────────────────────────


class TestReadThemeBytesNolink:
    def test_unsafe_slug_fails_closed(self, themes_dir: Path) -> None:
        target = themes_dir / "x" / "theme.json"
        _write_json(target, {})
        assert th._read_theme_bytes_nolink("../escape", target) is None

    def test_reads_a_regular_file_inside_the_pack(self, themes_dir: Path) -> None:
        target = themes_dir / "lcars" / "theme.json"
        _write_json(target, {"slug": "lcars"})
        raw = th._read_theme_bytes_nolink("lcars", target)
        assert raw is not None and b"lcars" in raw


# ── _do_install ────────────────────────────────────────────────────────────


class TestDoInstallRefusals:
    def test_unknown_source_type(self, themes_dir: Path) -> None:
        theme, err, status = th._do_install("ftp", {})
        assert theme is None
        assert err == "source.type must be 'local' or 'github'"
        assert status == 400

    def test_local_source_error_is_a_400(self, themes_dir: Path) -> None:
        theme, err, status = th._do_install("local", {"path": ""})
        assert theme is None and status == 400
        assert err == "local 'path' is required"

    def test_github_source_error_is_a_400(self, themes_dir: Path) -> None:
        theme, err, status = th._do_install("github", {"url": "http://github.com/o/r"})
        assert theme is None and status == 400
        assert err is not None and "only https" in err

    def test_github_sandbox_unavailable_is_a_503(
        self, themes_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            th,
            "_clone_github",
            lambda url, dest: th._THEME_GIT_SANDBOX_UNAVAILABLE,
        )

        theme, err, status = th._do_install("github", {"url": "https://github.com/o/r"})

        assert theme is None and err == th._THEME_GIT_SANDBOX_UNAVAILABLE
        assert status == 503


class TestDoInstallPromotion:
    def test_source_containing_the_themes_dir_is_rejected(self, themes_dir: Path) -> None:
        # The themes directory lives under KIROCREW_HOME, so installing FROM
        # that home would make the staging copy recurse into its own output.
        home = themes_dir.parent
        theme, err, status = th._do_install("local", {"path": str(home)})
        assert theme is None and status == 400
        assert err == "source directory must not contain the themes directory"

    def test_invalid_pack_is_rejected_without_staging_residue(
        self, themes_dir: Path, tmp_path: Path
    ) -> None:
        src = tmp_path / "broken"
        src.mkdir()
        _write_json(src / "theme.json", {"name": "No Level"})
        theme, err, status = th._do_install("local", {"path": str(src)})
        assert theme is None and status == 400 and err
        assert list(themes_dir.glob(".install-staging-*")) == []

    @requires_symlinks
    def test_symlinked_subdirectory_is_refused(self, themes_dir: Path, tmp_path: Path) -> None:
        src = _make_pack(tmp_path / "packlink")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        # An EXISTING directory target, so os.walk reports it under dirnames —
        # that is the branch that refuses a symlinked subdirectory outright.
        (src / "styles").symlink_to(elsewhere, target_is_directory=True)
        theme, err, status = th._do_install("local", {"path": str(src)})
        assert theme is None and status == 400
        assert err is not None and "symlinked directory" in err
        assert list(themes_dir.glob(".install-staging-*")) == []

    @requires_symlinks
    def test_non_regular_entry_is_refused(self, themes_dir: Path, tmp_path: Path) -> None:
        src = _make_pack(tmp_path / "packdangle")
        # A dangling symlink is walked as a FILE entry, so it is refused by the
        # regular-file check rather than the symlinked-directory check.
        (src / "readme.md").symlink_to(tmp_path / "missing-target")
        theme, err, status = th._do_install("local", {"path": str(src)})
        assert theme is None and status == 400
        assert err is not None and "non-regular file" in err
        assert list(themes_dir.glob(".install-staging-*")) == []

    def test_promotes_a_valid_pack(self, themes_dir: Path, tmp_path: Path) -> None:
        src = _make_pack(tmp_path / "pack")
        theme, err, status = th._do_install("local", {"path": str(src)})
        assert err is None and status == 200 and theme is not None
        assert theme["slug"] == "lcars"
        assert theme["source"] == "local"
        assert theme["level"] == 0
        assert (themes_dir / "lcars" / "theme.json").is_file()
        # No staging or backup residue is left behind.
        assert [p.name for p in themes_dir.iterdir()] == ["lcars"]

    def test_reinstall_replaces_the_installed_pack(
        self, themes_dir: Path, tmp_path: Path
    ) -> None:
        src = _make_pack(tmp_path / "pack")
        assert th._do_install("local", {"path": str(src)})[2] == 200
        _write_text(src / "readme.md", "second revision\n")
        theme, err, status = th._do_install("local", {"path": str(src)})
        assert err is None and status == 200 and theme is not None
        assert (themes_dir / "lcars" / "readme.md").is_file()
        assert [p.name for p in themes_dir.iterdir()] == ["lcars"]

    def test_custom_record_with_the_same_slug_is_409(
        self, themes_dir: Path, tmp_path: Path
    ) -> None:
        _write_json(themes_dir / "lcars.json", {"name": "LCARS"})
        src = _make_pack(tmp_path / "pack")
        theme, err, status = th._do_install("local", {"path": str(src)})
        assert theme is None and status == 409
        assert err == "a custom theme named 'lcars' already exists"
        assert list(themes_dir.glob(".install-staging-*")) == []

    def test_installing_the_installed_directory_onto_itself_is_rejected(
        self, themes_dir: Path, tmp_path: Path
    ) -> None:
        src = _make_pack(tmp_path / "pack")
        assert th._do_install("local", {"path": str(src)})[2] == 200
        theme, err, status = th._do_install(
            "local", {"path": str(themes_dir / "lcars")}
        )
        assert theme is None and status == 400
        assert err == "source is already the installed theme directory"


# ── POST /api/themes/install ───────────────────────────────────────────────


class TestApiThemesInstall:
    @pytest.mark.asyncio
    async def test_policy_denial_is_403_and_audited(
        self,
        themes_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _Denied:
            permitted = False
            rule = "capabilities.theme_install"
            layer = "policy"
            reason = "theme installs are disabled here"

        audits: list[tuple[str, str]] = []
        monkeypatch.setattr(gov_mod, "governance_permits", lambda *a, **k: _Denied())
        monkeypatch.setattr(
            th,
            "_audit_theme_install_governance",
            lambda outcome, decision, reason="": audits.append((outcome, reason)),
        )
        resp = await th.api_themes_install(_request("POST", "/api/themes/install"))
        assert resp.status == 403
        assert _body(resp)["error"] == "theme installs are disabled here"
        assert audits == [("denied", "")]

    @pytest.mark.asyncio
    async def test_governance_failure_fails_closed(
        self,
        themes_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(*a: object, **k: object) -> object:
            raise RuntimeError("governance backend down")

        audits: list[tuple[str, str]] = []
        monkeypatch.setattr(gov_mod, "governance_permits", _boom)
        monkeypatch.setattr(
            th,
            "_audit_theme_install_governance",
            lambda outcome, decision, reason="": audits.append((outcome, reason)),
        )
        resp = await th.api_themes_install(_request("POST", "/api/themes/install"))
        assert resp.status == 403
        assert _body(resp)["error"] == "theme installation blocked (governance unavailable)"
        assert audits == [("denied", "governance unavailable (fail-closed)")]

    @pytest.mark.asyncio
    async def test_malformed_json_is_400(
        self, themes_dir: Path, allow_install: None
    ) -> None:
        resp = await th.api_themes_install(
            _request("POST", "/api/themes/install", body=None)
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid JSON"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [{}, {"source": "local"}, ["nope"]])
    async def test_missing_source_object_is_400(
        self,
        payload: object,
        themes_dir: Path,
        allow_install: None,
    ) -> None:
        resp = await th.api_themes_install(
            _request("POST", "/api/themes/install", body=payload)
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "missing 'source' object"

    @pytest.mark.asyncio
    async def test_worker_error_and_status_pass_through(
        self,
        themes_dir: Path,
        allow_install: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            th, "_do_install", lambda stype, source: (None, "nope", 409)
        )
        resp = await th.api_themes_install(
            _request(
                "POST",
                "/api/themes/install",
                body={"source": {"type": "local", "path": "/x"}},
            )
        )
        assert resp.status == 409
        assert _body(resp)["error"] == "nope"

    @pytest.mark.asyncio
    async def test_sandbox_unavailable_has_retryable_status_and_code(
        self,
        themes_dir: Path,
        allow_install: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            th,
            "_do_install",
            lambda stype, source: (None, th._THEME_GIT_SANDBOX_UNAVAILABLE, 503),
        )

        resp = await th.api_themes_install(
            _request(
                "POST",
                "/api/themes/install",
                body={"source": {"type": "github", "url": "https://github.com/o/r"}},
            )
        )

        assert resp.status == 503
        assert _body(resp) == {
            "error": th._THEME_GIT_SANDBOX_UNAVAILABLE,
            "code": "theme_install_sandbox_unavailable",
        }

    @pytest.mark.asyncio
    async def test_success_returns_the_descriptor(
        self,
        themes_dir: Path,
        allow_install: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        descriptor = {
            "slug": "lcars",
            "name": "LCARS",
            "emoji": "🖖",
            "level": 0,
            "source": "local",
        }
        monkeypatch.setattr(
            th, "_do_install", lambda stype, source: (descriptor, None, 200)
        )
        resp = await th.api_themes_install(
            _request(
                "POST",
                "/api/themes/install",
                body={"source": {"type": "local", "path": "/x"}},
            )
        )
        assert resp.status == 200
        assert _body(resp) == {"ok": True, "slug": "lcars", "theme": descriptor}


# ── /api/themes/{slug} ─────────────────────────────────────────────────────


def _detail(method: str, slug: str, *, body: object = ...) -> web.Request:
    return _request(
        method, f"/api/themes/{slug}", body=body, match_info={"slug": slug}
    )


class TestApiThemeDetailSlugGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("slug", ["", "Upper", "../etc", "a/b", "a.b", "sp ace"])
    async def test_unsafe_slug_is_400(self, slug: str, themes_dir: Path) -> None:
        resp = await th.api_theme_detail(_detail("GET", slug))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid theme slug"


class TestApiThemeDetailStatsOffLoop:
    """#5963: the detail handler's target stats must never run on the event loop.

    On a UNC data home each ``exists()``/``is_dir()`` is SMB-backed and can
    block for as long as the network takes; a stat on the loop stalls every
    other request the gateway serves. Spy on ``Path.exists``/``Path.is_dir``
    for the handler's two target paths and assert every such stat happened on
    a worker thread — the same discipline #5943 pinned for the asset routes'
    offloaded ``_resolve_theme_asset``.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    async def test_target_stats_run_on_a_worker_thread(
        self, method: str, themes_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_json(themes_dir / "sunset.json", {"name": "Sunset"})
        # Build the watched set through the handler's own resolver: the
        # fixture path is unresolved, while ``_themes_dir()`` goes through
        # ``config_dir()``'s ``resolve()`` — on Darwin ``/tmp`` is a symlink,
        # so comparing against the raw fixture path would never match.
        watched = {th._themes_dir() / "sunset.json", th._themes_dir() / "sunset"}
        loop_thread = threading.get_ident()
        stat_threads: list[int] = []
        real_exists, real_is_dir = Path.exists, Path.is_dir

        def spy_exists(self: Path, *args: Any, **kwargs: Any) -> bool:
            if self in watched:
                stat_threads.append(threading.get_ident())
            return real_exists(self, *args, **kwargs)

        def spy_is_dir(self: Path, *args: Any, **kwargs: Any) -> bool:
            if self in watched:
                stat_threads.append(threading.get_ident())
            return real_is_dir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "exists", spy_exists)
        monkeypatch.setattr(Path, "is_dir", spy_is_dir)

        body = _theme_body() if method == "PUT" else ...
        resp = await th.api_theme_detail(_detail(method, "sunset", body=body))
        assert resp.status == 200
        assert stat_threads, "expected the handler to stat its target paths"
        on_loop = [t for t in stat_threads if t == loop_thread]
        assert not on_loop, f"{len(on_loop)} target stat(s) ran on the event loop"


class TestApiThemeDetailDelete:
    @pytest.mark.asyncio
    async def test_removes_a_custom_record(self, themes_dir: Path) -> None:
        _write_json(themes_dir / "sunset.json", {"name": "Sunset"})
        resp = await th.api_theme_detail(_detail("DELETE", "sunset"))
        assert resp.status == 200 and _body(resp) == {"ok": True}
        assert not (themes_dir / "sunset.json").exists()

    @pytest.mark.asyncio
    async def test_removes_an_installed_pack(self, themes_dir: Path) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_detail(_detail("DELETE", "lcars"))
        assert resp.status == 200 and _body(resp) == {"ok": True}
        assert not (themes_dir / "lcars").exists()

    @pytest.mark.asyncio
    async def test_unknown_slug_is_404(self, themes_dir: Path) -> None:
        resp = await th.api_theme_detail(_detail("DELETE", "ghost"))
        assert resp.status == 404
        assert _body(resp)["error"] == "not found"


class TestApiThemeDetailPut:
    @pytest.mark.asyncio
    async def test_installed_pack_is_read_only(self, themes_dir: Path) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_detail(_detail("PUT", "lcars", body=_theme_body()))
        assert resp.status == 400
        assert _body(resp)["error"] == "installed themes are read-only; reinstall to update"

    @pytest.mark.asyncio
    async def test_unknown_slug_is_404(self, themes_dir: Path) -> None:
        resp = await th.api_theme_detail(_detail("PUT", "ghost", body=_theme_body()))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_malformed_json_is_400(self, themes_dir: Path) -> None:
        _write_json(themes_dir / "sunset.json", {"name": "Sunset"})
        resp = await th.api_theme_detail(_detail("PUT", "sunset", body=None))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_validation_error_is_surfaced(self, themes_dir: Path) -> None:
        _write_json(themes_dir / "sunset.json", {"name": "Sunset"})
        resp = await th.api_theme_detail(
            _detail("PUT", "sunset", body={"name": "Sunset", "dark": {}})
        )
        assert resp.status == 400
        assert "missing required" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_update_preserves_created_at(self, themes_dir: Path) -> None:
        _write_json(
            themes_dir / "sunset.json",
            {"name": "Old", "created_at": "2026-01-01T00:00:00+00:00"},
        )
        resp = await th.api_theme_detail(
            _detail("PUT", "sunset", body=_theme_body("New Name"))
        )
        assert resp.status == 200
        theme = _body(resp)["theme"]
        assert theme["name"] == "New Name"
        assert theme["slug"] == "sunset"
        assert theme["created_at"] == "2026-01-01T00:00:00+00:00"
        assert json.loads((themes_dir / "sunset.json").read_text("utf-8")) == theme

    @pytest.mark.asyncio
    async def test_corrupt_existing_record_gets_a_fresh_created_at(
        self, themes_dir: Path
    ) -> None:
        _write_text(themes_dir / "sunset.json", "{ not json")
        resp = await th.api_theme_detail(_detail("PUT", "sunset", body=_theme_body()))
        assert resp.status == 200
        assert _body(resp)["theme"]["created_at"]

    @pytest.mark.asyncio
    async def test_blank_emoji_falls_back_to_the_default(self, themes_dir: Path) -> None:
        _write_json(themes_dir / "sunset.json", {"name": "Sunset"})
        resp = await th.api_theme_detail(
            _detail("PUT", "sunset", body=_theme_body(emoji=" "))
        )
        assert _body(resp)["theme"]["emoji"] == th._THEME_DEFAULT_EMOJI


class TestApiThemeDetailGet:
    @pytest.mark.asyncio
    async def test_returns_the_custom_record_verbatim(self, themes_dir: Path) -> None:
        record = {"name": "Sunset", "slug": "sunset", "emoji": "🌇", **_VALID_VARS}
        _write_json(themes_dir / "sunset.json", record)
        resp = await th.api_theme_detail(_detail("GET", "sunset"))
        assert resp.status == 200
        assert _body(resp) == record

    @pytest.mark.asyncio
    async def test_corrupt_record_is_500(self, themes_dir: Path) -> None:
        _write_text(themes_dir / "sunset.json", "{{{")
        resp = await th.api_theme_detail(_detail("GET", "sunset"))
        assert resp.status == 500
        assert _body(resp)["error"] == "failed to read theme"

    @pytest.mark.asyncio
    async def test_installed_pack_detail_carries_level_and_assets(
        self, themes_dir: Path
    ) -> None:
        pack = _make_pack(themes_dir / "lcars", level=1)
        manifest_path = pack / "theme.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["loaderIcons"] = ["star", "sparkles", "moon", "cloud"]
        _write_json(manifest_path, manifest)
        resp = await th.api_theme_detail(_detail("GET", "lcars"))
        assert resp.status == 200
        payload = _body(resp)
        assert payload["slug"] == "lcars"
        assert payload["source"] == "installed"
        assert payload["level"] == 1
        assert payload["dark"]["--bg"] == "#000000"
        assert payload["assets"]["loaderIcons"] == manifest["loaderIcons"]

    @pytest.mark.asyncio
    async def test_invalid_installed_pack_is_500(self, themes_dir: Path) -> None:
        # A directory with a manifest but no formatVersion fails validation on
        # the READ path too — the route reports 500 rather than a silent empty.
        _write_json(themes_dir / "lcars" / "theme.json", {"name": "LCARS"})
        resp = await th.api_theme_detail(_detail("GET", "lcars"))
        assert resp.status == 500
        # The client body carries a generic message + machine code; the raw
        # validation detail (which can name the on-disk theme dir) stays in the
        # server log, not the verbatim-rendered `error` field.
        body = _body(resp)
        assert body["error"] == "invalid installed theme"
        assert body["code"] == "invalid_installed_theme"

    @pytest.mark.asyncio
    async def test_manifest_read_failure_falls_back_to_an_empty_manifest(
        self,
        themes_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_pack(themes_dir / "lcars")
        monkeypatch.setattr(th, "_read_json_file", lambda *a, **k: (None, "boom"))
        resp = await th.api_theme_detail(_detail("GET", "lcars"))
        assert resp.status == 200
        assert _body(resp)["slug"] == "lcars"

    @pytest.mark.asyncio
    async def test_unknown_slug_is_404(self, themes_dir: Path) -> None:
        resp = await th.api_theme_detail(_detail("GET", "ghost"))
        assert resp.status == 404


# ── asset / overlay / topbar serving ───────────────────────────────────────


class TestThemeHtmlResponse:
    def test_carries_the_sandbox_csp_and_nosniff(self) -> None:
        resp = th._theme_html_response("<div>hi</div>")
        assert resp.content_type == "text/html"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Content-Security-Policy"] == th._THEME_OVERLAY_CSP


def _asset_request(slug: str, path: str) -> web.Request:
    return _request(
        "GET",
        f"/api/theme/{slug}/assets/{path}",
        match_info={"slug": slug, "path": path},
    )


class TestApiThemeAsset:
    @pytest.mark.asyncio
    async def test_unsafe_slug_is_400(self, themes_dir: Path) -> None:
        resp = await th.api_theme_asset(_asset_request("../etc", "logo.svg"))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid theme slug"

    @pytest.mark.asyncio
    async def test_missing_asset_is_404(self, themes_dir: Path) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_asset(_asset_request("lcars", "branding/logo.svg"))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unsupported_extension_is_400(self, themes_dir: Path) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "notes.txt", "hello\n")
        resp = await th.api_theme_asset(_asset_request("lcars", "notes.txt"))
        assert resp.status == 400
        assert _body(resp)["error"] == "unsupported asset type"

    @pytest.mark.asyncio
    async def test_unreadable_bytes_are_404(
        self,
        themes_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "branding" / "logo.svg", "<svg/>")
        monkeypatch.setattr(th, "_read_theme_bytes_nolink", lambda slug, target: None)
        resp = await th.api_theme_asset(_asset_request("lcars", "branding/logo.svg"))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_serves_the_asset_with_a_locked_down_csp(
        self, themes_dir: Path
    ) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "branding" / "logo.svg", "<svg/>")
        resp = await th.api_theme_asset(_asset_request("lcars", "branding/logo.svg"))
        assert resp.status == 200
        assert resp.body == b"<svg/>"
        assert resp.content_type == "image/svg+xml"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Content-Security-Policy"] == th._THEME_ASSET_CSP


def _overlay_request(slug: str, oid: str) -> web.Request:
    return _request(
        "GET",
        f"/api/theme/{slug}/overlay/{oid}",
        match_info={"slug": slug, "id": oid},
    )


class TestApiThemeOverlay:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("oid", ["", "../etc", "a/b", "a.b"])
    async def test_unsafe_overlay_id_is_400(
        self, oid: str, themes_dir: Path
    ) -> None:
        resp = await th.api_theme_overlay(_overlay_request("lcars", oid))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid overlay id"

    @pytest.mark.asyncio
    async def test_unsafe_slug_is_400(self, themes_dir: Path) -> None:
        resp = await th.api_theme_overlay(_overlay_request("Bad", "scanner"))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid theme slug"

    @pytest.mark.asyncio
    async def test_missing_overlay_is_404(self, themes_dir: Path) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_overlay(_overlay_request("lcars", "scanner"))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unreadable_overlay_is_404(
        self,
        themes_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "overlays" / "scanner.html", "<div/>")
        monkeypatch.setattr(th, "_read_theme_bytes_nolink", lambda slug, target: None)
        resp = await th.api_theme_overlay(_overlay_request("lcars", "scanner"))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_serves_overlay_html_sandboxed(self, themes_dir: Path) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(
            themes_dir / "lcars" / "overlays" / "scanner.html", "<div>scan</div>"
        )
        resp = await th.api_theme_overlay(_overlay_request("lcars", "SCANNER"))
        assert resp.status == 200
        assert resp.text == "<div>scan</div>"
        assert resp.headers["Content-Security-Policy"] == th._THEME_OVERLAY_CSP


def _topbar_request(slug: str, mode: str) -> web.Request:
    return _request(
        "GET",
        f"/api/theme/{slug}/topbar/{mode}",
        match_info={"slug": slug, "mode": mode},
    )


class TestApiThemeTopbar:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["", "DARK", "sepia", "../dark"])
    async def test_unknown_mode_is_400(
        self, mode: str, themes_dir: Path
    ) -> None:
        resp = await th.api_theme_topbar(_topbar_request("lcars", mode))
        assert resp.status == 400
        assert _body(resp)["error"] == "mode must be dark or light"

    @pytest.mark.asyncio
    async def test_unsafe_slug_is_400(self, themes_dir: Path) -> None:
        resp = await th.api_theme_topbar(_topbar_request("Bad", "dark"))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid theme slug"

    @pytest.mark.asyncio
    async def test_missing_topbar_is_404(self, themes_dir: Path) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_topbar(_topbar_request("lcars", "light"))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unreadable_topbar_is_404(
        self,
        themes_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "topbar" / "dark.html", "<div/>")
        monkeypatch.setattr(th, "_read_theme_bytes_nolink", lambda slug, target: None)
        resp = await th.api_theme_topbar(_topbar_request("lcars", "dark"))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_serves_topbar_html_sandboxed(self, themes_dir: Path) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "topbar" / "dark.html", "<div>bar</div>")
        resp = await th.api_theme_topbar(_topbar_request("lcars", "dark"))
        assert resp.status == 200
        assert resp.text == "<div>bar</div>"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"


class TestResolveLocalSourceAncestorLinks:
    r"""An ancestor link is refused before the path is ever resolved.

    The UNC screen in `_resolve_local_source` is lexical, so it only sees the
    path it was handed. A path that looks entirely local but sits BENEATH a
    link to `\\attacker\share` therefore passes it, and the leaf
    `is_link_or_junction` check tests only the final component. The first
    `is_dir()` is what resolves the chain -- and on Windows resolving it is
    what authenticates to the attacker's host. These tests pin the ancestor
    screen that has to run before that call.
    """

    @requires_symlinks
    def test_a_local_path_beneath_a_linked_ancestor_is_refused(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(th, "IS_WINDOWS", True)
        real = tmp_path / "real"
        real.mkdir()
        (real / "theme").mkdir()
        link = tmp_path / "via"
        link.symlink_to(real, target_is_directory=True)

        # Entirely local-looking, and the LEAF is a real directory: neither the
        # UNC screen nor the leaf link check fires. Only the ancestor walk does.
        src, err = th._resolve_local_source(str(link / "theme"))

        assert src is None
        assert err == "local path must not be a symlink"

    @requires_symlinks
    def test_the_error_does_not_disclose_which_ancestor_was_linked(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(th, "IS_WINDOWS", True)
        real = tmp_path / "real"
        real.mkdir()
        (real / "theme").mkdir()
        link = tmp_path / "secretname"
        link.symlink_to(real, target_is_directory=True)

        _src, err = th._resolve_local_source(str(link / "theme"))

        assert "secretname" not in (err or "")

    @requires_symlinks
    def test_the_walk_runs_BEFORE_the_leaf_probe(self, tmp_path, monkeypatch):
        """The leaf probe is itself an lstat, which resolves every ancestor.

        `is_link_or_junction(p)` does not follow the FINAL component, but it
        still resolves the ones above it -- so running it before the ancestor
        walk would traverse the very junction the walk exists to refuse, and
        make the SMB connection anyway. Wiring the leaf predicate to raise
        proves the walk returned first: if the order regresses, this explodes
        instead of failing an assertion.
        """
        real = tmp_path / "real"
        real.mkdir()
        (real / "theme").mkdir()
        link = tmp_path / "via"
        link.symlink_to(real, target_is_directory=True)

        monkeypatch.setattr(th, "IS_WINDOWS", True)

        def _boom(_path):
            raise AssertionError("leaf probe ran before the ancestor walk")

        # Patches themes' own binding only; `first_linked_ancestor` resolves the
        # predicate through platform_compat, so the walk itself still works.
        monkeypatch.setattr(th, "is_link_or_junction", _boom)

        src, err = th._resolve_local_source(str(link / "theme"))

        assert src is None
        assert err == "local path must not be a symlink"

    @requires_symlinks
    def test_a_posix_path_beneath_a_linked_ancestor_is_still_accepted(
        self, tmp_path, monkeypatch
    ):
        """macOS `/tmp` and `/var` are symlinks to `/private/*`.

        An unconditional ancestor walk refuses every install from a temp dir on
        macOS -- a platform this change does not otherwise touch. The walk is
        Windows-only because the harm it prevents is that the PROBE is the
        attack, which is a UNC property; on POSIX the real guard is
        `is_sensitive_path` applied to the RESOLVED path. `tmp_path` is already
        resolved, so the linked ancestor has to be built explicitly here or the
        regression hides.
        """
        monkeypatch.setattr(th, "IS_WINDOWS", False)
        real = tmp_path / "private_real"
        real.mkdir()
        (real / "theme").mkdir()
        link = tmp_path / "tmp_like"
        link.symlink_to(real, target_is_directory=True)

        src, err = th._resolve_local_source(str(link / "theme"))

        assert err is None
        assert src == (real / "theme").resolve()

    def test_an_unlinked_local_path_is_still_accepted(self, tmp_path):
        # The screen must not reject ordinary nesting -- otherwise it "fixes"
        # the vector by breaking every real local install.
        nested = tmp_path / "a" / "b" / "theme"
        nested.mkdir(parents=True)

        src, err = th._resolve_local_source(str(nested))

        assert err is None
        assert src == nested.resolve()


class TestFirstLinkedAncestor:
    """Root-first ordering is the safety property, so it is pinned directly."""

    def test_it_returns_none_when_no_ancestor_is_a_link(self, tmp_path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert platform_compat.first_linked_ancestor(nested) is None

    @requires_symlinks
    def test_it_finds_a_linked_ancestor(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "via"
        link.symlink_to(real, target_is_directory=True)
        assert platform_compat.first_linked_ancestor(link / "leaf") == str(link)

    @requires_symlinks
    def test_the_leaf_itself_is_not_reported(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "via"
        link.symlink_to(real, target_is_directory=True)
        # The leaf is is_link_or_junction's job; double-reporting it here would
        # make the two checks return different errors for the same condition.
        assert platform_compat.first_linked_ancestor(link) is None

    @requires_symlinks
    def test_it_reports_the_OUTERMOST_link_when_several_are_nested(self, tmp_path):
        outer_real = tmp_path / "outer_real"
        (outer_real / "mid").mkdir(parents=True)
        outer = tmp_path / "outer"
        outer.symlink_to(outer_real, target_is_directory=True)
        inner_real = outer_real / "inner_real"
        inner_real.mkdir()
        (outer_real / "mid" / "inner").symlink_to(inner_real, target_is_directory=True)

        got = platform_compat.first_linked_ancestor(outer / "mid" / "inner" / "leaf")

        # Root-first: stopping at the OUTER link means no lstat was issued
        # through it to discover the inner one.
        assert got == str(outer)
