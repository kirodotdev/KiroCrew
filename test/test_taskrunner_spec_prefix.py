"""``taskrunner._read_spec_prefix`` reads the spec through the descriptor gate.

``start_background`` validates the spec path with ``hooks.validate_file_path`` and
then hands the validated NAME to ``_read_spec_prefix``. A read that re-opens that
name is not a read of what was validated: a hardlink alias shares its target's
inode but carries its own innocent name, so ``realpath`` yields the alias,
``is_symlink()`` is False, every name-based check passes — and the bytes belong
to whatever it aliases. ``st_nlink`` is the only signal, and it is readable only
on an open descriptor, so the read goes through
``hooks.safe_read_file_bytes_nolink`` (open first, validate the descriptor, read
that same descriptor) with the spec's own directory as ``within_root``.

The caller's failure shape is unchanged: an unreadable spec yields an empty
prefix, never an error that would tell a caller whether a path is protected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.taskrunner import _read_spec_prefix


def _plant_alias(secret: Path, alias: Path) -> None:
    try:
        os.link(secret, alias)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - host capability
        pytest.skip(f"filesystem does not support hardlinks: {exc}")
    if alias.stat().st_nlink < 2:  # pragma: no cover - host capability
        pytest.skip("filesystem did not create a second link")


def _protected_secret(tmp_path: Path, monkeypatch) -> Path:
    # Path.home() reads USERPROFILE on Windows and never HOME; pin both.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from kiro_crew.security import is_sensitive_path

    secret = tmp_path / ".aws" / "credentials"
    secret.parent.mkdir()
    secret.write_text("aws_secret_access_key = SHOULD-NOT-APPEAR\n", encoding="utf-8")
    assert is_sensitive_path(str(secret)), "precondition: the target is protected"
    return secret


class TestReadSpecPrefix:
    def test_reads_a_plain_spec_prefix(self, tmp_path: Path):
        spec = tmp_path / "task.md"
        spec.write_text("  # Build the thing\n\nStep one.\n  ", encoding="utf-8")
        assert _read_spec_prefix(str(spec), 4000) == "# Build the thing\n\nStep one."

    def test_bounds_the_prefix_by_characters_not_bytes(self, tmp_path: Path):
        # Multi-byte UTF-8: the bound is in characters, as the text-mode read
        # it replaces counted, and a cut inside a code point is never published.
        spec = tmp_path / "task.md"
        spec.write_text("こんにちは" * 1000, encoding="utf-8")
        out = _read_spec_prefix(str(spec), 7)
        assert out == "こんにちはこん"

    def test_normalizes_newlines_like_the_text_mode_read_it_replaces(self, tmp_path: Path):
        spec = tmp_path / "task.md"
        spec.write_bytes(b"# Title\r\n\r\nStep one.\rStep two.\r\n")
        assert _read_spec_prefix(str(spec), 4000) == "# Title\n\nStep one.\nStep two."

    def test_invalid_utf8_still_raises_for_the_caller_to_map(self, tmp_path: Path):
        # The caller maps any error to an empty prefix; keep that contract strict
        # rather than silently publishing replacement characters.
        spec = tmp_path / "task.md"
        spec.write_bytes(b"# Title\xff\xfe\n")
        with pytest.raises(UnicodeDecodeError):
            _read_spec_prefix(str(spec), 4000)

    def test_an_incomplete_sequence_at_eof_still_raises(self, tmp_path: Path):
        # Only a byte-cap cut may leave a dangling code point; a file that ENDS
        # mid code point is malformed and must not become a shorter prefix.
        spec = tmp_path / "task.md"
        spec.write_bytes("# Title こ".encode("utf-8")[:-1])
        with pytest.raises(UnicodeDecodeError):
            _read_spec_prefix(str(spec), 4000)

    def test_a_byte_cap_cut_mid_code_point_is_not_an_error(self, tmp_path: Path):
        # 3-byte code points against a 4-bytes-per-char cap: the cut lands
        # inside a code point, and the bound still yields exactly max_chars.
        spec = tmp_path / "task.md"
        spec.write_text("こ" * 100, encoding="utf-8")
        assert _read_spec_prefix(str(spec), 5) == "こ" * 5

    def test_a_missing_spec_yields_an_empty_prefix(self, tmp_path: Path):
        assert _read_spec_prefix(str(tmp_path / "ghost.md"), 4000) == ""

    def test_a_directory_yields_an_empty_prefix(self, tmp_path: Path):
        assert _read_spec_prefix(str(tmp_path), 4000) == ""

    def test_a_protected_name_yields_an_empty_prefix(self, tmp_path: Path, monkeypatch):
        secret = _protected_secret(tmp_path, monkeypatch)
        assert _read_spec_prefix(str(secret), 4000) == ""

    def test_withholds_a_hardlink_alias_of_a_protected_file(self, tmp_path: Path, monkeypatch):
        """The alias validates as an innocent spec name; the inode is the secret."""
        secret = _protected_secret(tmp_path, monkeypatch)
        alias = tmp_path / "specs" / "task.md"
        alias.parent.mkdir()
        _plant_alias(secret, alias)

        assert _read_spec_prefix(str(alias), 4000) == ""

    @requires_symlinks
    def test_withholds_a_symlink_to_a_protected_file(self, tmp_path: Path, monkeypatch):
        secret = _protected_secret(tmp_path, monkeypatch)
        link = tmp_path / "specs" / "task.md"
        link.parent.mkdir()
        link.symlink_to(secret)

        assert _read_spec_prefix(str(link), 4000) == ""
