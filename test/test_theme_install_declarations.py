"""Theme manifest declarations, the frontend asset descriptor and font roles.

theme.json ``overlays`` and ``topbar`` declarations (§3.1), the audio manifest
(§3.3), the per-rule overrides.css layout denylist (§4.2), the placement and
behaviour the asset descriptor reports from those declarations, a font face's
``role`` and the font-pin rule, and a pack installed before that rule. The store and the tier-aware validation core are in
``test_theme_install.py``; the shared pack builders are in ``theme_install_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from theme_install_helpers import _VALID_MP3, _VALID_VARS, _make_tiered, _write

from kiro_crew.dashboard.theme_validate import (
    _THEME_OVERLAY_DEFAULT_POSITION,
    _THEME_OVERLAY_DEFAULT_ZINDEX,
    _overrides_layout_violation,
    _theme_asset_descriptor,
    _validate_audio_manifest,
    _validate_overlay_decls,
    _validate_overrides_css,
    _validate_theme_dir,
    _validate_topbar_decls,
)

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

    def test_behind_layer_passes(self, tmp_path: Path) -> None:
        decl = dict(_FULL_OVERLAY, layer="behind")
        assert _validate_overlay_decls({"overlays": [decl]}, self._pack(tmp_path)) is None

    @pytest.mark.parametrize("layer", ["under", [], {}, 1])
    def test_bad_layer_rejected(self, tmp_path: Path, layer: object) -> None:
        # Unhashable values must be a validation error, not a TypeError (HTTP 500).
        err = _validate_overlay_decls(
            {"overlays": [dict(_FULL_OVERLAY, layer=layer)]}, self._pack(tmp_path)
        )
        assert err is not None and "invalid layer" in err

    def test_clickable_behind_layer_rejected(self, tmp_path: Path) -> None:
        err = _validate_overlay_decls(
            {"overlays": [dict(_FULL_OVERLAY, layer="behind", pointerEvents=True)]},
            self._pack(tmp_path),
        )
        assert err is not None and "behind layer must not set pointerEvents" in err

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
        tb = {
            "topbar": {"dark": "topbar/dark.html", "light": "topbar/light.html", "height": height}
        }
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
            "triggers": {
                "notification": {"src": "audio/chime.mp3", "volume": 0.5, "maxDuration": 2}
            },
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
        assert desc["overlays"][0]["layer"] == "above"

    def test_behind_layer_reaches_descriptor(self, tmp_path: Path) -> None:
        d = _decl_pack(tmp_path, {"overlays/web.html": "<div>w</div>"})
        manifest = {
            "level": 2,
            "name": "W",
            "overlays": [{"id": "web", "src": "overlays/web.html", "layer": "behind"}],
        }
        desc = _theme_asset_descriptor(d, manifest, 2)
        assert desc["overlays"][0]["layer"] == "behind"


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
        ] + [{"family": "M", "file": f"m{w}.ttf", "weight": w, "role": "mono"} for w in (400, 500)]
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
            'body::before{content:"";position:fixed;inset:0;pointer-events:none}',
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
