"""The libcups probe (``browser_harness/libcups.py``) against ELF files built in the test."""

from __future__ import annotations

import struct
from collections.abc import Iterable
from pathlib import Path

import pytest

from kiro_crew.browser_harness import libcups

_X86_64 = 62
_AARCH64 = 183
_SHT_STRTAB = 3
_SHT_DYNSYM = 11
_SHT_PROGBITS = 1
_TEXT_INDEX = 3
_GLOBAL_FUNC = 0x12

#: What Amazon Linux 2's cups-libs 1.6.3 exports, as far as this probe cares:
#: ordinary libcups functions and none of the four the full build needs.
_AL2_EXPORTS = ("ippNew", "cupsGetDests", "httpConnectEncrypt")
_CURRENT_EXPORTS = (*libcups.REQUIRED_SYMBOLS, "ippNew", "cupsGetDests")


def _elf(
    defined: Iterable[str] = (),
    undefined: Iterable[str] = (),
    *,
    elf_class: int = 2,
    machine: int = _X86_64,
) -> bytes:
    """A little-endian ELF shared object carrying only ``.dynstr`` and ``.dynsym``."""
    names = b"\0"
    symbols = [struct.pack("<IBBHQQ", 0, 0, 0, 0, 0, 0)]
    entries = [(name, _TEXT_INDEX) for name in defined] + [(name, 0) for name in undefined]
    for name, shndx in entries:
        offset = len(names)
        names += name.encode("ascii") + b"\0"
        value = 0x1000 if shndx else 0
        symbols.append(struct.pack("<IBBHQQ", offset, _GLOBAL_FUNC, 0, shndx, value, 0))
    dynsym = b"".join(symbols)
    dynstr_off = 64
    dynsym_off = dynstr_off + len(names)
    pad = (-dynsym_off) % 8
    dynsym_off += pad
    shoff = dynsym_off + len(dynsym)
    shoff += (-shoff) % 8
    sections = [
        struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        struct.pack("<IIQQQQIIQQ", 0, _SHT_STRTAB, 0, 0, dynstr_off, len(names), 0, 0, 1, 0),
        struct.pack("<IIQQQQIIQQ", 0, _SHT_DYNSYM, 0, 0, dynsym_off, len(dynsym), 1, 1, 8, 24),
        struct.pack("<IIQQQQIIQQ", 0, _SHT_PROGBITS, 6, 0, 0, 0, 0, 0, 16, 0),
    ]
    ident = b"\x7fELF" + bytes([elf_class, 1, 1]) + b"\0" * 9
    header = ident + struct.pack(
        "<HHIQQQIHHHHHH", 3, machine, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(sections), 0
    )
    body = header + names + b"\0" * pad + dynsym
    body += b"\0" * (shoff - len(body))
    return body + b"".join(sections)


def _lib_dir(tmp_path: Path, payload: bytes, name: str = "lib64") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    (directory / libcups.LIBCUPS).write_bytes(payload)
    return directory


def test_a_libcups_that_defines_every_required_symbol_is_current(tmp_path: Path) -> None:
    lib = _lib_dir(tmp_path, _elf(defined=_CURRENT_EXPORTS))
    assert libcups.missing_symbols([lib], machine="x86_64") == frozenset()
    assert libcups.too_old_for_chrome([lib], system="linux", machine="x86_64") is False


def test_amazon_linux_2s_libcups_is_too_old(tmp_path: Path) -> None:
    lib = _lib_dir(tmp_path, _elf(defined=_AL2_EXPORTS))
    assert libcups.missing_symbols([lib], machine="x86_64") == frozenset(libcups.REQUIRED_SYMBOLS)
    assert libcups.too_old_for_chrome([lib], system="linux", machine="x86_64") is True


def test_one_missing_symbol_is_enough(tmp_path: Path) -> None:
    present = [name for name in _CURRENT_EXPORTS if name != "cupsUserAgent"]
    lib = _lib_dir(tmp_path, _elf(defined=present))
    assert libcups.missing_symbols([lib], machine="x86_64") == {"cupsUserAgent"}
    assert libcups.too_old_for_chrome([lib], system="linux", machine="x86_64") is True


def test_a_symbol_the_library_only_imports_is_missing(tmp_path: Path) -> None:
    present = [name for name in _CURRENT_EXPORTS if name != "ippValidateAttributes"]
    lib = _lib_dir(tmp_path, _elf(defined=present, undefined=["ippValidateAttributes"]))
    assert libcups.missing_symbols([lib], machine="x86_64") == {"ippValidateAttributes"}


def test_no_libcups_is_no_evidence(tmp_path: Path) -> None:
    assert libcups.missing_symbols([tmp_path / "nothing"], machine="x86_64") is None
    assert libcups.too_old_for_chrome([tmp_path / "nothing"], system="linux") is False


@pytest.mark.parametrize(
    "payload",
    [b"", b"not an elf at all", b"\x7fELF\x02\x01\x01" + b"\0" * 9 + b"\x03\x00"],
    ids=["empty", "not-elf", "truncated"],
)
def test_an_unreadable_library_is_no_evidence(tmp_path: Path, payload: bytes) -> None:
    lib = _lib_dir(tmp_path, payload)
    assert libcups.missing_symbols([lib], machine="x86_64") is None
    assert libcups.too_old_for_chrome([lib], system="linux", machine="x86_64") is False


def test_a_32_bit_or_foreign_copy_is_passed_over_for_the_next_directory(tmp_path: Path) -> None:
    thirty_two = _lib_dir(tmp_path, _elf(defined=_AL2_EXPORTS, elf_class=1), "lib")
    foreign = _lib_dir(tmp_path, _elf(defined=_AL2_EXPORTS, machine=_AARCH64), "aarch64")
    native = _lib_dir(tmp_path, _elf(defined=_CURRENT_EXPORTS), "lib64")
    dirs = [thirty_two, foreign, native]
    assert libcups.missing_symbols(dirs, machine="x86_64") == frozenset()
    assert libcups.missing_symbols([foreign], machine="aarch64") == frozenset(
        libcups.REQUIRED_SYMBOLS
    )


def test_the_first_directory_holding_a_native_copy_decides(tmp_path: Path) -> None:
    old = _lib_dir(tmp_path, _elf(defined=_AL2_EXPORTS), "usr-lib64")
    current = _lib_dir(tmp_path, _elf(defined=_CURRENT_EXPORTS), "lib64")
    assert libcups.too_old_for_chrome([old, current], system="linux", machine="x86_64") is True
    assert libcups.too_old_for_chrome([current, old], system="linux", machine="x86_64") is False


@pytest.mark.parametrize("system", ["darwin", "win32"])
def test_only_linux_is_ever_too_old(tmp_path: Path, system: str) -> None:
    lib = _lib_dir(tmp_path, _elf(defined=_AL2_EXPORTS))
    assert libcups.too_old_for_chrome([lib], system=system, machine="x86_64") is False


def test_the_default_search_reads_only_the_system_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LD_LIBRARY_PATH`` can name a directory the agent writes; it never decides."""
    planted = _lib_dir(tmp_path, _elf(defined=_AL2_EXPORTS), "planted")
    monkeypatch.setenv("LD_LIBRARY_PATH", str(planted))
    monkeypatch.setattr(libcups, "LIBRARY_DIRS", (str(tmp_path / "system"),))
    assert libcups.missing_symbols(machine="x86_64") is None
