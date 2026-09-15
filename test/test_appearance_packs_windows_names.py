"""Windows-name validation for pack ids and pack member filenames.

The two validators in ``appearance_packs.store`` are the only boundary between a pack
manifest (or a route parameter) and a real path on disk, so a name Windows
treats specially has to be refused there or not at all.

Two distinct Win32 behaviours are covered:

  * **Reserved device names.** ``CON`` ``PRN`` ``AUX`` ``NUL`` ``COM1-9``
    ``LPT1-9`` are devices, and the reservation applies to the STEM -- ``nul.svg``
    is the NUL device just like ``nul``. Opening one for writing SUCCEEDS and
    discards the bytes, so a pack whose ``idle`` state pointed at ``nul.svg``
    would import "successfully" and then render nothing, with no error anywhere
    to explain it. Creating a directory named for one fails instead.
  * **A trailing dot.** NTFS drops it, so ``idle.`` and ``idle`` are the same
    file on Windows and different files on POSIX.

Both are refused on every platform, not just Windows: packs move between
machines, and a validator whose answer depends on the host would let a macOS
export produce a bundle no Windows user can import.
"""

from __future__ import annotations

import pytest

from kiro_crew.appearance_packs import store as ap

_RESERVED_IDS = ["con", "CON", "nul", "NUL", "prn", "aux", "com1", "COM9", "lpt1", "lpt9"]
_RESERVED_FILES = ["nul.svg", "NUL.svg", "con.json", "aux.png", "com1.svg", "lpt9.svg"]


class TestReservedPackIds:
    @pytest.mark.parametrize("ident", _RESERVED_IDS)
    def test_a_reserved_device_name_is_refused_as_a_pack_id(self, ident: str) -> None:
        # A pack id becomes a directory name; mkdir("nul") fails on Windows, so the
        # import must be refused at validation rather than half-completed on disk.
        assert ap._safe_id(ident) is None, f"accepted {ident!r}"

    @pytest.mark.parametrize("ident", ["connect", "console", "communication", "auxiliary"])
    def test_a_name_that_merely_starts_with_a_device_name_is_kept(self, ident: str) -> None:
        # Over-rejecting would break legitimate packs. Only an exact stem match is
        # reserved -- `connect` is an ordinary directory name on every OS.
        assert ap._safe_id(ident) == ident

    @pytest.mark.parametrize("ident", ["cat", "mine", "ghost-pack", "pack_2", "com10"])
    def test_the_ids_real_packs_use_are_still_accepted(self, ident: str) -> None:
        # `com10` is deliberately here: Win32 reserves COM1-COM9 only.
        assert ap._safe_id(ident) == ident


class TestReservedPackFilenames:
    @pytest.mark.parametrize("name", _RESERVED_FILES)
    def test_a_reserved_device_stem_is_refused_as_a_member_name(self, name: str) -> None:
        # This is the silent-data-loss case: the write would succeed and vanish.
        assert ap._safe_filename(name) is None, f"accepted {name!r}"

    @pytest.mark.parametrize("name", ["idle.", "manifest.json.", "sleep."])
    def test_a_trailing_dot_is_refused(self, name: str) -> None:
        # Windows strips it, collapsing `idle.` onto `idle` and overwriting a
        # sibling member that passed validation as a separate file.
        assert ap._safe_filename(name) is None, f"accepted {name!r}"

    @pytest.mark.parametrize(
        "name",
        ["idle.svg", "manifest.json", "random-happy.png", "console.svg", "com10.svg"],
    )
    def test_the_names_real_packs_use_are_still_accepted(self, name: str) -> None:
        assert ap._safe_filename(name) == name
