"""Name validation for pack ids and pack member filenames.

The two validators in ``appearance_packs.store`` are the only boundary between a pack
manifest (or a route parameter) and a real path on disk, so a name Windows
treats specially has to be refused there or not at all.

Three distinct behaviours are covered:

  * **Reserved device names.** ``CON`` ``PRN`` ``AUX`` ``NUL`` ``COM1-9``
    ``LPT1-9`` are devices, and the reservation applies to the STEM -- ``nul.svg``
    is the NUL device just like ``nul``. Opening one for writing SUCCEEDS and
    discards the bytes, so a pack whose ``idle`` state pointed at ``nul.svg``
    would import "successfully" and then render nothing, with no error anywhere
    to explain it. Creating a directory named for one fails instead.
  * **A trailing dot.** NTFS drops it, so ``idle.`` and ``idle`` are the same
    file on Windows and different files on POSIX.
  * **A name that escapes the pack.** ``_safe_filename`` once rejected
    separators with a denylist, so ``C:evil.json`` passed -- no ``/``, no ``\\``,
    no ``..`` -- and on Windows ``Path("packs/x") / "C:evil.json"`` resolves to
    ``C:evil.json``, outside the pack entirely.

All are refused on every platform, not just Windows: packs move between
machines, and a validator whose answer depends on the host would let a macOS
export produce a bundle no Windows user can import.
"""

from __future__ import annotations

import pytest

from kiro_crew.appearance_packs import store as ap

_RESERVED_IDS = ["con", "CON", "nul", "NUL", "prn", "aux", "com1", "COM9", "lpt1", "lpt9"]
_RESERVED_FILES = ["nul.svg", "NUL.svg", "con.json", "aux.png", "com1.svg", "lpt9.svg"]
_TRAILING_DOT_FILES = ["idle.", "manifest.json.", "sleep."]
_ESCAPING_FILES = [
    "C:evil.json",  # drive prefix, the reported escape
    "C:/evil.json",
    "manifest.json:stream",  # NTFS alternate data stream
    "../evil.svg",
    "/etc/passwd",
    "sub\\dir.svg",
    ".hidden",
    "star*.svg",
    "quest?.svg",
    "pipe|.svg",
    "nul\x00.svg",
    "",
    "   ",
    "x" * 129,
]


def _cases(old_name: str, values: list[str]) -> list:
    return [pytest.param(v, id=f"{old_name}-{v!r}") for v in values]


class TestReservedPackIds:
    @pytest.mark.parametrize("ident", _RESERVED_IDS)
    def test_a_reserved_device_name_is_refused_as_a_pack_id(self, ident: str) -> None:
        # A pack id becomes a directory name; mkdir("nul") fails on Windows, so the
        # import must be refused at validation rather than half-completed on disk.
        assert ap._safe_id(ident) is None, f"accepted {ident!r}"

    @pytest.mark.parametrize(
        "ident",
        # Over-rejecting would break legitimate packs: only an exact stem match is
        # reserved, so `connect` is an ordinary directory name on every OS, and
        # `com10` is here because Win32 reserves COM1-COM9 only.
        _cases(
            "test_a_name_that_merely_starts_with_a_device_name_is_kept",
            ["connect", "console", "communication", "auxiliary"],
        )
        + _cases(
            "test_the_ids_real_packs_use_are_still_accepted",
            ["cat", "mine", "ghost-pack", "pack_2", "com10"],
        ),
    )
    def test_an_ordinary_pack_id_is_accepted(self, ident: str) -> None:
        assert ap._safe_id(ident) == ident


class TestPackMemberFilenames:
    @pytest.mark.parametrize(
        "name",
        # Reserved stems are the silent-data-loss case: the write would succeed and
        # vanish. A trailing dot is stripped by Windows, collapsing `idle.` onto
        # `idle` and overwriting a sibling that passed validation separately.
        _cases("test_a_reserved_device_stem_is_refused_as_a_member_name", _RESERVED_FILES)
        + _cases("test_a_trailing_dot_is_refused", _TRAILING_DOT_FILES)
        + _cases("test_a_name_that_could_escape_is_refused", _ESCAPING_FILES),
    )
    def test_an_unsafe_member_name_is_refused(self, name: str) -> None:
        assert ap._safe_filename(name) is None, f"accepted {name!r}"

    @pytest.mark.parametrize(
        "name",
        # The guard must not be so strict that a legitimate pack stops importing.
        _cases(
            "test_the_names_real_packs_use_are_still_accepted",
            ["idle.svg", "manifest.json", "random-happy.png", "console.svg", "com10.svg"],
        )
        + _cases(
            "test_the_names_real_packs_use_are_accepted",
            ["idle.svg", "manifest.json", "random-happy.png", "sleep_mask.svg", "a1.svg"],
        ),
    )
    def test_an_ordinary_member_name_is_accepted(self, name: str) -> None:
        assert ap._safe_filename(name) == name
