"""Coverage tests for ``kiro_crew.dashboard.handlers.themes`` — HTTP surface.

``test_theme_install.py`` covers the pure validation core plus a few module
helpers; this file covers the parts a validator test never reaches: the six
aiohttp handlers (list / create / install / detail / asset / overlay / topbar),
the blocking workers they offload to the discovery pool (``_list_themes_sync``,
``_do_install``), the local/GitHub source resolvers, and the refusal branches —
invalid JSON, slug traversal, governance denial, read-only installed packs,
unsupported asset types, and the honest 501 the pack routes return on Windows.

Every test points ``KIROCREW_HOME`` at ``tmp_path`` so ``_themes_dir()``
resolves inside the sandbox: nothing is written outside it. No network, no git,
no real subprocess — ``_clone_github``'s spawn is replaced with a stub so only
its URL guard and error mapping are exercised.

Platform notes: the pack routes are gated behind ``_THEMES_WIN_UNSUPPORTED``
because the install/serve reads funnel through the POSIX-only nolink chokepoint
(``safe_read_file_bytes_nolink``). Tests that only need the non-nolink half pin
that flag to ``False`` so they run on Windows too; tests that genuinely need the
chokepoint (install promotion, asset bytes, symlink refusals) are skipped there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import kiro_crew.platform.governance_profiles as gov_mod
from kiro_crew.dashboard.handlers import themes as th

_NOT_POSIX = os.name == "nt"
_posix_only = pytest.mark.skipif(
    _NOT_POSIX, reason="needs the POSIX-only nolink read chokepoint / symlinks"
)

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
def pack_routes_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the platform gate off so the non-nolink half runs on Windows too."""
    monkeypatch.setattr(th, "_THEMES_WIN_UNSUPPORTED", False)


@pytest.fixture
def pack_routes_win(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the platform gate on to exercise the honest-501 branches anywhere."""
    monkeypatch.setattr(th, "_THEMES_WIN_UNSUPPORTED", True)


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


# ── the Windows gate ───────────────────────────────────────────────────────


class TestWinUnsupportedResponse:
    def test_is_501_naming_the_tracking_issue(self) -> None:
        resp = th._win_unsupported_response()
        assert resp.status == 501
        assert "Windows" in _body(resp)["error"]
        assert "#311" in _body(resp)["error"]


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

    @_posix_only
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
        # The pre-lock check only sees <slug>.json; the in-lock check is what
        # refuses a slug already taken by an installed <slug>/ directory.
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

    @_posix_only
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


# ── _atomic_write_theme_json ───────────────────────────────────────────────


class TestAtomicWriteFailureCleanup:
    def test_a_failed_temp_unlink_still_reraises_the_original_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_unlink(_path: object) -> None:
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

    @_posix_only
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


@_posix_only
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
    async def test_windows_returns_an_honest_501(self, pack_routes_win: None) -> None:
        resp = await th.api_themes_install(_request("POST", "/api/themes/install"))
        assert resp.status == 501

    @pytest.mark.asyncio
    async def test_policy_denial_is_403_and_audited(
        self,
        themes_dir: Path,
        pack_routes_enabled: None,
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
        pack_routes_enabled: None,
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
        self, themes_dir: Path, pack_routes_enabled: None, allow_install: None
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
        pack_routes_enabled: None,
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
        pack_routes_enabled: None,
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
    async def test_success_returns_the_descriptor(
        self,
        themes_dir: Path,
        pack_routes_enabled: None,
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


class TestApiThemeDetailDelete:
    @pytest.mark.asyncio
    async def test_removes_a_custom_record(self, themes_dir: Path) -> None:
        _write_json(themes_dir / "sunset.json", {"name": "Sunset"})
        resp = await th.api_theme_detail(_detail("DELETE", "sunset"))
        assert resp.status == 200 and _body(resp) == {"ok": True}
        assert not (themes_dir / "sunset.json").exists()

    @pytest.mark.asyncio
    async def test_removes_an_installed_pack(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_detail(_detail("DELETE", "lcars"))
        assert resp.status == 200 and _body(resp) == {"ok": True}
        assert not (themes_dir / "lcars").exists()

    @pytest.mark.asyncio
    async def test_pack_removal_is_501_on_windows(
        self, themes_dir: Path, pack_routes_win: None
    ) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_detail(_detail("DELETE", "lcars"))
        assert resp.status == 501
        assert (themes_dir / "lcars").is_dir()

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
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_detail(_detail("GET", "lcars"))
        assert resp.status == 200
        payload = _body(resp)
        assert payload["slug"] == "lcars"
        assert payload["source"] == "installed"
        assert payload["level"] == 0
        assert payload["dark"]["--bg"] == "#000000"
        assert "assets" in payload

    @pytest.mark.asyncio
    async def test_installed_pack_detail_is_501_on_windows(
        self, themes_dir: Path, pack_routes_win: None
    ) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_detail(_detail("GET", "lcars"))
        assert resp.status == 501

    @pytest.mark.asyncio
    async def test_invalid_installed_pack_is_500(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        # A directory with a manifest but no formatVersion fails validation on
        # the READ path too — the route reports 500 rather than a silent empty.
        _write_json(themes_dir / "lcars" / "theme.json", {"name": "LCARS"})
        resp = await th.api_theme_detail(_detail("GET", "lcars"))
        assert resp.status == 500
        assert _body(resp)["error"].startswith("invalid installed theme:")

    @pytest.mark.asyncio
    async def test_manifest_read_failure_falls_back_to_an_empty_manifest(
        self,
        themes_dir: Path,
        pack_routes_enabled: None,
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
    async def test_windows_returns_501(self, pack_routes_win: None) -> None:
        resp = await th.api_theme_asset(_asset_request("lcars", "branding/logo.svg"))
        assert resp.status == 501

    @pytest.mark.asyncio
    async def test_unsafe_slug_is_400(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        resp = await th.api_theme_asset(_asset_request("../etc", "logo.svg"))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid theme slug"

    @pytest.mark.asyncio
    async def test_missing_asset_is_404(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_asset(_asset_request("lcars", "branding/logo.svg"))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unsupported_extension_is_400(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "notes.txt", "hello\n")
        resp = await th.api_theme_asset(_asset_request("lcars", "notes.txt"))
        assert resp.status == 400
        assert _body(resp)["error"] == "unsupported asset type"

    @pytest.mark.asyncio
    async def test_unreadable_bytes_are_404(
        self,
        themes_dir: Path,
        pack_routes_enabled: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "branding" / "logo.svg", "<svg/>")
        monkeypatch.setattr(th, "_read_theme_bytes_nolink", lambda slug, target: None)
        resp = await th.api_theme_asset(_asset_request("lcars", "branding/logo.svg"))
        assert resp.status == 404

    @pytest.mark.asyncio
    @_posix_only
    async def test_serves_the_asset_with_a_locked_down_csp(
        self, themes_dir: Path, pack_routes_enabled: None
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
    async def test_windows_returns_501(self, pack_routes_win: None) -> None:
        resp = await th.api_theme_overlay(_overlay_request("lcars", "scanner"))
        assert resp.status == 501

    @pytest.mark.asyncio
    @pytest.mark.parametrize("oid", ["", "../etc", "a/b", "a.b"])
    async def test_unsafe_overlay_id_is_400(
        self, oid: str, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        resp = await th.api_theme_overlay(_overlay_request("lcars", oid))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid overlay id"

    @pytest.mark.asyncio
    async def test_unsafe_slug_is_400(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        resp = await th.api_theme_overlay(_overlay_request("Bad", "scanner"))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid theme slug"

    @pytest.mark.asyncio
    async def test_missing_overlay_is_404(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_overlay(_overlay_request("lcars", "scanner"))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unreadable_overlay_is_404(
        self,
        themes_dir: Path,
        pack_routes_enabled: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "overlays" / "scanner.html", "<div/>")
        monkeypatch.setattr(th, "_read_theme_bytes_nolink", lambda slug, target: None)
        resp = await th.api_theme_overlay(_overlay_request("lcars", "scanner"))
        assert resp.status == 404

    @pytest.mark.asyncio
    @_posix_only
    async def test_serves_overlay_html_sandboxed(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
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
    async def test_windows_returns_501(self, pack_routes_win: None) -> None:
        resp = await th.api_theme_topbar(_topbar_request("lcars", "dark"))
        assert resp.status == 501

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["", "DARK", "sepia", "../dark"])
    async def test_unknown_mode_is_400(
        self, mode: str, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        resp = await th.api_theme_topbar(_topbar_request("lcars", mode))
        assert resp.status == 400
        assert _body(resp)["error"] == "mode must be dark or light"

    @pytest.mark.asyncio
    async def test_unsafe_slug_is_400(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        resp = await th.api_theme_topbar(_topbar_request("Bad", "dark"))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid theme slug"

    @pytest.mark.asyncio
    async def test_missing_topbar_is_404(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        _make_pack(themes_dir / "lcars")
        resp = await th.api_theme_topbar(_topbar_request("lcars", "light"))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unreadable_topbar_is_404(
        self,
        themes_dir: Path,
        pack_routes_enabled: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "topbar" / "dark.html", "<div/>")
        monkeypatch.setattr(th, "_read_theme_bytes_nolink", lambda slug, target: None)
        resp = await th.api_theme_topbar(_topbar_request("lcars", "dark"))
        assert resp.status == 404

    @pytest.mark.asyncio
    @_posix_only
    async def test_serves_topbar_html_sandboxed(
        self, themes_dir: Path, pack_routes_enabled: None
    ) -> None:
        _make_pack(themes_dir / "lcars")
        _write_text(themes_dir / "lcars" / "topbar" / "dark.html", "<div>bar</div>")
        resp = await th.api_theme_topbar(_topbar_request("lcars", "dark"))
        assert resp.status == 200
        assert resp.text == "<div>bar</div>"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
