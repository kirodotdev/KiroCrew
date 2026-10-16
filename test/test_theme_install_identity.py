"""Theme slugs and pack identity.

The ``_slugify_theme_name`` hash fallback and its ASCII-part predicate, the string
an identity derives from, a legacy pack whose name filters to nothing, and the
total read of an installed pack's identity. The store and the tier-aware
validation core are in ``test_theme_install.py``; the shared pack builders are in
``theme_install_helpers.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

import pytest
from theme_install_helpers import _VALID_VARS, _make_theme, _write

import kiro_crew.dashboard.handlers.themes as th_mod
import kiro_crew.dashboard.theme_validate as tv_mod
from conftest import requires_symlinks
from kiro_crew.dashboard.theme_validate import (
    _THEME_FILE_CAPS,
    _safe_theme_slug,
    _slugify_theme_name,
    _validate_theme_dir,
)
from kiro_crew.slugs import slug_hash_fallback

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
            expected = tv_mod._theme_slug_ascii_part(name) or slug_hash_fallback(name, "custom")
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
        # flake-ok: pre-existing unbounded acquire of the module-global per-slug install lock, moved verbatim; tracked for a de-flake pass
        lock.acquire()
        worker = threading.Thread(target=_install, daemon=True)
        worker.start()
        try:
            # Cannot reach its decision while the lock is held.
            # flake-ok: pre-existing 2 s join that expires while the lock is held, before the still-blocked check, moved verbatim; tracked for a de-flake pass
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
