#!/usr/bin/env python3
"""Give the vendored Windows ggml DLLs private names.

Windows keeps one module per base name in a process: a DLL's imports are
resolved by module name against the modules already loaded before any
directory is searched, even when the importing DLL was opened by full path.
A vendored ``ggml.dll`` under upstream's name is therefore shared with any
other ``ggml.dll`` in the process (a source-built whisper.cpp, say), in
whichever order the two load.

This script is the Windows counterpart of the patchelf step the Linux
directories get (``src/kiro_crew/_vendor/README.md``). For ``ggml.dll``,
``ggml-base.dll`` and ``ggml-cpu.dll`` in one directory it:

* renames each file ``<stem>-<first 8 hex of its sha256 before patching>.dll``;
* in every DLL of the directory that imports one of them, appends one
  read-only section holding the new names and points the import
  descriptors' name fields at it.

Nothing else moves: the old name strings stay where they were (unreferenced),
so every existing section keeps its bytes except the 4-byte name fields, and
the change is undone exactly by :func:`restore`. ``llama.dll`` keeps its file
name, because ctypes opens it by that name.

The files it accepts are the ones it was written for: PE32+ images with no
Authenticode signature, no bound or delay-load imports and nothing after the
last section. Anything else is refused, never guessed at.

Usage, from the repository root::

    python scripts/rename_vendored_win_dlls.py                # rename in place
    python scripts/rename_vendored_win_dlls.py --restore-to DIR

``--restore-to`` writes the files as they were before the rename, under the
upstream names, into DIR. It changes nothing in the vendored directory.

Standard library only, and it never loads the files it edits, so it runs on
any host.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import struct
import sys
from dataclasses import dataclass

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DIR = _REPO_ROOT / "src" / "kiro_crew" / "_vendor" / "llama_cpp_libs" / "win_amd64"

#: Upstream's names for the files that get a private one.
PUBLIC_NAMES = ("ggml.dll", "ggml-base.dll", "ggml-cpu.dll")
#: The section the private names live in. At most 8 bytes, NUL padded.
NAMES_SECTION = b".kcname\x00"

_PRIVATE_NAME = re.compile(r"^(?P<stem>ggml(?:-base|-cpu)?)-(?P<digest>[0-9a-f]{8})\.dll$")
_PE32_PLUS = 0x20B
_SECTION_HEADER = struct.Struct("<8sIIIIIIHHI")
_IMPORT_DESCRIPTOR = struct.Struct("<IIIII")
#: IMAGE_SCN_CNT_INITIALIZED_DATA | IMAGE_SCN_MEM_READ
_READ_ONLY_DATA = 0x40000040
_DIR_EXPORT, _DIR_IMPORT, _DIR_SECURITY, _DIR_BOUND_IMPORT, _DIR_DELAY_IMPORT = 0, 1, 4, 11, 13


class UnsupportedImage(ValueError):
    """The file is not one this script knows how to edit."""


@dataclass(frozen=True)
class Section:
    name: bytes
    virtual_size: int
    virtual_address: int
    raw_size: int
    raw_offset: int
    characteristics: int


@dataclass(frozen=True)
class Import:
    """One import descriptor: the DLL it names and where that name is recorded."""

    name: str
    name_rva: int
    #: File offset of the descriptor's 4-byte name RVA.
    name_field: int


@dataclass(frozen=True)
class Image:
    pe_offset: int
    optional_offset: int
    section_table: int
    section_alignment: int
    file_alignment: int
    size_of_headers: int
    directories: tuple[tuple[int, int], ...]
    sections: tuple[Section, ...]
    imports: tuple[Import, ...]

    def offset(self, rva: int) -> int:
        """The file offset holding *rva*."""
        for section in self.sections:
            if section.virtual_address <= rva < section.virtual_address + section.raw_size:
                return section.raw_offset + rva - section.virtual_address
        raise UnsupportedImage(f"RVA {rva:#x} is in no section's file data")


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _c_string(data: bytes, offset: int) -> str:
    return data[offset : data.index(b"\x00", offset)].decode("ascii")


def read_image(data: bytes) -> Image:
    """Parse the headers, section table and import descriptors of a PE32+ file."""
    if data[:2] != b"MZ":
        raise UnsupportedImage("not a PE file")
    (pe_offset,) = struct.unpack_from("<I", data, 0x3C)
    if data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        raise UnsupportedImage("not a PE file")
    (section_count,) = struct.unpack_from("<H", data, pe_offset + 6)
    (optional_size,) = struct.unpack_from("<H", data, pe_offset + 20)
    optional_offset = pe_offset + 24
    if struct.unpack_from("<H", data, optional_offset)[0] != _PE32_PLUS:
        raise UnsupportedImage("not a PE32+ (64-bit) image")
    section_alignment, file_alignment = struct.unpack_from("<II", data, optional_offset + 32)
    (size_of_headers,) = struct.unpack_from("<I", data, optional_offset + 60)
    (directory_count,) = struct.unpack_from("<I", data, optional_offset + 108)
    directories = tuple(
        struct.unpack_from("<II", data, optional_offset + 112 + 8 * index)
        for index in range(directory_count)
    )
    section_table = optional_offset + optional_size
    sections = []
    for index in range(section_count):
        name, vsize, vaddr, rsize, roffset, _, _, _, _, flags = _SECTION_HEADER.unpack_from(
            data, section_table + _SECTION_HEADER.size * index
        )
        sections.append(Section(name, vsize, vaddr, rsize, roffset, flags))
    image = Image(
        pe_offset,
        optional_offset,
        section_table,
        section_alignment,
        file_alignment,
        size_of_headers,
        directories,
        tuple(sections),
        (),
    )
    imports = []
    if len(directories) > _DIR_IMPORT and directories[_DIR_IMPORT][0]:
        entry = image.offset(directories[_DIR_IMPORT][0])
        while True:
            lookup, _, _, name_rva, address_table = _IMPORT_DESCRIPTOR.unpack_from(data, entry)
            if not (lookup or name_rva or address_table):
                break
            imports.append(Import(_c_string(data, image.offset(name_rva)), name_rva, entry + 12))
            entry += _IMPORT_DESCRIPTOR.size
    return Image(
        pe_offset,
        optional_offset,
        section_table,
        section_alignment,
        file_alignment,
        size_of_headers,
        directories,
        tuple(sections),
        tuple(imports),
    )


def export_table(data: bytes) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """The exported names and the function RVAs of a PE32+ file, in table order."""
    image = read_image(data)
    rva, _ = image.directories[_DIR_EXPORT]
    if not rva:
        return (), ()
    _, _, _, _, _, _, function_count, name_count, functions, names, _ = struct.unpack_from(
        "<IIHHIIIIIII", data, image.offset(rva)
    )
    exported = tuple(
        _c_string(
            data, image.offset(struct.unpack_from("<I", data, image.offset(names) + 4 * index)[0])
        )
        for index in range(name_count)
    )
    addresses = struct.unpack_from(f"<{function_count}I", data, image.offset(functions))
    return exported, tuple(addresses)


def _require_supported(data: bytes, image: Image) -> None:
    def present(index: int) -> bool:
        return len(image.directories) > index and image.directories[index] != (0, 0)

    if present(_DIR_SECURITY):
        raise UnsupportedImage(
            "the file carries an Authenticode signature; editing it would break it"
        )
    if present(_DIR_BOUND_IMPORT) or present(_DIR_DELAY_IMPORT):
        raise UnsupportedImage(
            "bound or delay-load imports name DLLs in tables this script does not rewrite"
        )
    end_of_sections = max(section.raw_offset + section.raw_size for section in image.sections)
    if len(data) != end_of_sections or len(data) % image.file_alignment:
        raise UnsupportedImage("data after the last section would be displaced by an added section")


def private_name(public_name: str, data: bytes) -> str:
    """``<stem>-<first 8 hex of the sha256 of the unpatched file>.dll``."""
    return f"{public_name[: -len('.dll')]}-{hashlib.sha256(data).hexdigest()[:8]}.dll"


def patch(data: bytes, names: dict[str, str]) -> bytes:
    """*data* with every import of a key of *names* renamed to its value.

    The keys are lower-case public names. A file importing none of them is
    returned unchanged.
    """
    image = read_image(data)
    _require_supported(data, image)
    renamed = [entry for entry in image.imports if entry.name.lower() in names]
    if not renamed:
        return data
    if any(section.name == NAMES_SECTION for section in image.sections):
        raise UnsupportedImage("the file already has a private-names section")

    out = bytearray(data)
    last = max(image.sections, key=lambda section: section.virtual_address)
    section_rva = _align(last.virtual_address + last.virtual_size, image.section_alignment)
    strings = bytearray()
    for entry in renamed:
        struct.pack_into("<I", out, entry.name_field, section_rva + len(strings))
        strings += names[entry.name.lower()].encode("ascii") + b"\x00"
    raw_size = _align(len(strings), image.file_alignment)

    header = image.section_table + _SECTION_HEADER.size * len(image.sections)
    first_raw = min(section.raw_offset for section in image.sections if section.raw_offset)
    if header + _SECTION_HEADER.size > min(image.size_of_headers, first_raw) or any(
        out[header : header + _SECTION_HEADER.size]
    ):
        raise UnsupportedImage("no free slot in the section table")
    _SECTION_HEADER.pack_into(
        out,
        header,
        NAMES_SECTION,
        len(strings),
        section_rva,
        raw_size,
        len(data),
        0,
        0,
        0,
        0,
        _READ_ONLY_DATA,
    )
    struct.pack_into("<H", out, image.pe_offset + 6, len(image.sections) + 1)
    (initialized,) = struct.unpack_from("<I", out, image.optional_offset + 8)
    struct.pack_into("<I", out, image.optional_offset + 8, initialized + raw_size)
    struct.pack_into(
        "<I",
        out,
        image.optional_offset + 56,
        _align(section_rva + len(strings), image.section_alignment),
    )
    return bytes(out) + bytes(strings) + b"\x00" * (raw_size - len(strings))


def _public_name(name: str) -> str | None:
    """Upstream's name for a private ggml name, or None for any other name."""
    match = _PRIVATE_NAME.match(name)
    return f"{match['stem']}.dll" if match else None


def restore(data: bytes) -> bytes:
    """Undo :func:`patch`: the bytes the file had before its imports were renamed.

    A file without the private-names section is returned unchanged.
    """
    image = read_image(data)
    added = image.sections[-1]
    if added.name != NAMES_SECTION:
        if any(section.name == NAMES_SECTION for section in image.sections):
            raise UnsupportedImage("the private-names section is not the last one")
        return data
    if added.raw_offset + added.raw_size != len(data):
        raise UnsupportedImage("the private-names section does not end the file")

    out = bytearray(data[: added.raw_offset])
    for entry in image.imports:
        if not added.virtual_address <= entry.name_rva < added.virtual_address + added.virtual_size:
            continue
        public = _public_name(entry.name)
        if public is None:
            raise UnsupportedImage(f"{entry.name!r} is not a private ggml name")
        # patch() leaves the public name where the linker put it, so it is
        # found by content. A DLL never imports itself, so its own name in its
        # export table is a different string from every name looked up here.
        needle = b"\x00" + public.encode("ascii") + b"\x00"
        found = [match.start() + 1 for match in re.finditer(re.escape(needle), bytes(out))]
        if len(found) != 1:
            raise UnsupportedImage(
                f"{public!r} appears {len(found)} times; expected its one import name"
            )
        offset = found[0]
        section = next(
            s for s in image.sections if s.raw_offset <= offset < s.raw_offset + s.raw_size
        )
        struct.pack_into(
            "<I", out, entry.name_field, section.virtual_address + offset - section.raw_offset
        )

    remaining = image.sections[:-1]
    header = image.section_table + _SECTION_HEADER.size * len(remaining)
    out[header : header + _SECTION_HEADER.size] = bytes(_SECTION_HEADER.size)
    struct.pack_into("<H", out, image.pe_offset + 6, len(remaining))
    (initialized,) = struct.unpack_from("<I", out, image.optional_offset + 8)
    struct.pack_into("<I", out, image.optional_offset + 8, initialized - added.raw_size)
    last = max(remaining, key=lambda section: section.virtual_address)
    struct.pack_into(
        "<I",
        out,
        image.optional_offset + 56,
        _align(last.virtual_address + last.virtual_size, image.section_alignment),
    )
    return bytes(out)


def restored_files(directory: pathlib.Path) -> dict[str, bytes]:
    """Every DLL of *directory* as it was before the rename, keyed by its upstream name."""
    return {
        _public_name(path.name) or path.name: restore(path.read_bytes())
        for path in sorted(directory.glob("*.dll"))
    }


def rename_in_place(directory: pathlib.Path) -> dict[str, str]:
    """Rename the public ggml files of *directory* and repoint every importer.

    Returns ``{public name: private name}``. Refuses a directory that does not
    hold all three public names, which is what an already renamed one looks like.
    """
    missing = [name for name in PUBLIC_NAMES if not (directory / name).is_file()]
    if missing:
        raise UnsupportedImage(f"{directory} has no {', '.join(missing)}; is it already renamed?")
    # Every name comes from its file's bytes BEFORE any file is patched.
    names = {name: private_name(name, (directory / name).read_bytes()) for name in PUBLIC_NAMES}
    patched = {path: patch(path.read_bytes(), names) for path in sorted(directory.glob("*.dll"))}
    for path, data in patched.items():
        path.write_bytes(data)
        if path.name in names:
            path.rename(path.with_name(names[path.name]))
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("directory", nargs="?", type=pathlib.Path, default=DEFAULT_DIR)
    parser.add_argument(
        "--restore-to",
        type=pathlib.Path,
        metavar="DIR",
        help="write the files as they were before the rename into DIR, and change nothing else",
    )
    args = parser.parse_args(argv)
    try:
        if args.restore_to is not None:
            args.restore_to.mkdir(parents=True, exist_ok=True)
            for name, data in restored_files(args.directory).items():
                (args.restore_to / name).write_bytes(data)
                print(f"{hashlib.sha256(data).hexdigest()}  {name}")
            return 0
        for public, private in rename_in_place(args.directory).items():
            print(f"{public} -> {private}")
    except UnsupportedImage as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
