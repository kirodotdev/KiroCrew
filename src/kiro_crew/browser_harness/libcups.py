"""Whether this host's libcups can load the full Chrome for Testing build.

The full build (``chrome``) links ``libcups.so.2`` and binds every symbol at load
time, so a system copy that lacks a function it calls stops it before it reads a
byte of CDP: Amazon Linux 2 ships cups-libs 1.6.3, which defines none of
:data:`REQUIRED_SYMBOLS`, and the binary exits 127 with ``symbol lookup error:
undefined symbol: ippValidateAttributes``. Chrome for Testing also publishes a
``chrome-headless-shell`` build of every version, which does not link libcups at
all; :mod:`.chromium` downloads and launches that one on such a host.

The probe reads the filesystem and nothing else: ``install_state`` and the status
poll reach it, and both promise to launch nothing. The library is looked for in
the dynamic linker's system directories (:data:`LIBRARY_DIRS`), never through
``LD_LIBRARY_PATH``, which can name a directory the agent writes; its dynamic
symbol table is parsed here, so no binutils is needed. A library outside those
directories, or one that cannot be read, is no evidence, and no evidence keeps
the full build.
"""

from __future__ import annotations

import platform as _platform
import struct
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

#: The libcups functions the full Chrome for Testing build at
#: ``chromium.CHROMIUM_VERSION`` imports: every libcups reference in its dynamic
#: symbol table that cups-libs 1.6.3 does not define. A version bump re-reads the
#: list with ``nm -D --undefined-only chrome-linux64/chrome``.
REQUIRED_SYMBOLS: tuple[str, ...] = (
    "ippValidateAttributes",
    "cupsFindDestDefault",
    "cupsFindDestSupported",
    "cupsUserAgent",
)

#: The soname the full build links against.
LIBCUPS = "libcups.so.2"

#: The dynamic linker's system directories, 64-bit first: the RPM layout, then the
#: Debian multiarch one, then the flat one. The first that holds a native 64-bit
#: copy decides.
LIBRARY_DIRS: tuple[str, ...] = (
    "/usr/lib64",
    "/lib64",
    "/usr/lib/x86_64-linux-gnu",
    "/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    "/lib/aarch64-linux-gnu",
    "/usr/lib",
    "/lib",
)

#: ``e_machine`` for each host ``platform.machine()`` Chrome for Testing builds for,
#: so a cross or 32-bit copy is passed over for the next directory.
_ELF_MACHINES: dict[str, int] = {"x86_64": 62, "amd64": 62, "aarch64": 183, "arm64": 183}

_ELF_MAGIC = b"\x7fELF"
_ELF_IDENT_BYTES = 16
_ELFCLASS64 = 2
_ELFDATA2LSB = 1
_SHT_DYNSYM = 11
_SHN_UNDEF = 0
#: The ELF64 header after ``e_ident``, a section header, and a symbol, little-endian.
_EHDR = struct.Struct("<HHIQQQIHHHHHH")
_SHDR = struct.Struct("<IIQQQQIIQQ")
_SYM = struct.Struct("<IBBHQQ")
#: Upper bound on one table read: libcups' are a few dozen KiB; a damaged header
#: must not make the status poll read a whole disk.
_MAX_TABLE_BYTES = 16 << 20


def too_old_for_chrome(
    dirs: Iterable[Path | str] | None = None,
    *,
    system: str | None = None,
    machine: str | None = None,
) -> bool:
    """Whether the full build cannot load here: Linux, and the libcups read lacks a symbol.

    ``False`` off Linux, and whenever there is no evidence (no libcups found, or
    none readable).
    """
    if not (system or sys.platform).startswith("linux"):
        return False
    missing = missing_symbols(dirs, machine=machine)
    return bool(missing)


def missing_symbols(
    dirs: Iterable[Path | str] | None = None, *, machine: str | None = None
) -> frozenset[str] | None:
    """The :data:`REQUIRED_SYMBOLS` the system libcups does not define.

    ``None`` when no native libcups could be read in *dirs* (default
    :data:`LIBRARY_DIRS`). Never raises.
    """
    wanted = _ELF_MACHINES.get((machine or _platform.machine()).lower())
    if wanted is None:
        return None
    required = frozenset(REQUIRED_SYMBOLS)
    for directory in LIBRARY_DIRS if dirs is None else dirs:
        defined = _defined_symbols(Path(directory) / LIBCUPS, required, wanted)
        if defined is not None:
            return required - defined
    return None


def _defined_symbols(path: Path, names: frozenset[str], machine: int) -> frozenset[str] | None:
    """Which of *names* the ELF file at *path* defines in its dynamic symbol table.

    ``None`` when *path* is not a readable little-endian 64-bit ELF file for
    *machine* with a ``.dynsym`` section.
    """
    try:
        with path.open("rb") as fh:
            ident = fh.read(_ELF_IDENT_BYTES)
            if (
                len(ident) != _ELF_IDENT_BYTES
                or ident[:4] != _ELF_MAGIC
                or ident[4] != _ELFCLASS64
                or ident[5] != _ELFDATA2LSB
            ):
                return None
            header = _EHDR.unpack(_read_exact(fh, _EHDR.size))
            e_machine, e_shoff, e_shentsize, e_shnum = header[1], header[5], header[10], header[11]
            if e_machine != machine or not e_shoff or not e_shnum or e_shentsize != _SHDR.size:
                return None
            sections = _SHDR.iter_unpack(_read_at(fh, e_shoff, e_shnum * _SHDR.size))
            table = [(sh[1], sh[4], sh[5], sh[6], sh[9]) for sh in sections]
            for sh_type, offset, size, link, entsize in table:
                if sh_type != _SHT_DYNSYM:
                    continue
                if link >= len(table) or entsize != _SYM.size:
                    return None
                strings = _read_at(fh, table[link][1], table[link][2])
                symbols = _read_at(fh, offset, size - size % _SYM.size)
                return frozenset(_defined_names(symbols, strings, names))
    except (OSError, struct.error, ValueError):
        return None
    return None


def _defined_names(symbols: bytes, strings: bytes, names: frozenset[str]) -> set[str]:
    found: set[str] = set()
    for st_name, _info, _other, st_shndx, _value, _size in _SYM.iter_unpack(symbols):
        if st_shndx == _SHN_UNDEF:
            continue
        end = strings.find(b"\0", st_name)
        if end < 0:
            continue
        name = strings[st_name:end].decode("ascii", "replace")
        if name in names:
            found.add(name)
    return found


def _read_at(fh: BinaryIO, offset: int, size: int) -> bytes:
    if size > _MAX_TABLE_BYTES:
        raise ValueError("ELF table larger than any libcups carries")
    fh.seek(offset)
    return _read_exact(fh, size)


def _read_exact(fh: BinaryIO, size: int) -> bytes:
    data = fh.read(size)
    if len(data) != size:
        raise ValueError("ELF file ends inside a table")
    return data
