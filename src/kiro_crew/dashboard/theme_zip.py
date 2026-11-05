"""Unpack an uploaded theme ``.zip`` into a private directory for install.

The dashboard's local install source is a folder path on the GATEWAY host, so a
browser on another machine (a phone, a laptop reaching the gateway through a
tunnel) has no way to hand it a pack. ``POST /api/themes/install-zip`` takes the
archive bytes instead; this module turns them into a plain directory that the
existing install pipeline (stage -> validate -> promote) then treats exactly like
a local folder. Nothing here decides what a theme may contain: every allowlist,
size cap and content check stays in that pipeline.

What this module does refuse is the part only an ARCHIVE can express: an entry
that would land outside the extraction directory (absolute name, drive letter
or stream name in any component, ``..``, a path that resolves outside it), a
symlink or other non-regular entry, a member compressed with anything but
stored or deflate (``zipfile`` cannot bound a BZIP2 or LZMA read), an
inventory larger than the biggest tier
(vetted by ``kiro_crew.zip_vet`` before ``zipfile`` parses the directory), and
an archive whose declared or actual uncompressed size is larger than the
biggest pack the validator accepts.
Each file is written with a byte budget, so a header that under-reports its size
cannot inflate past the cap either.
"""

from __future__ import annotations

import io
import stat
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from kiro_crew.dashboard.theme_validate import (
    _THEME_ENTRIES_BY_LEVEL,
    _THEME_MANIFEST_NAME,
    _THEME_TOTAL_BYTES_BY_LEVEL,
)
from kiro_crew.zip_vet import ZipInventoryRejected, vet_zip_inventory_bytes

#: The largest upload accepted, compressed. A pack that fits the validator's
#: biggest total also fits this, since zip never grows a stored file by more than
#: its small per-entry header.
THEME_ZIP_MAX_UPLOAD_BYTES = max(_THEME_TOTAL_BYTES_BY_LEVEL.values()) + 1024 * 1024

#: The largest total the archive may unpack to.
_MAX_UNPACKED_BYTES = max(_THEME_TOTAL_BYTES_BY_LEVEL.values())

#: Entries beyond the validator's biggest tier, with headroom for the folder
#: entries and macOS metadata an archiver adds and this module skips.
_MAX_ENTRIES = 4 * max(_THEME_ENTRIES_BY_LEVEL.values())

#: The deepest valid file is styles/fonts/x.ttf (three parts), plus one
#: optional pack wrapper and two parts of headroom. Bound tree depth before
#: extraction so staging and recursive cleanup cannot exhaust the call stack.
_MAX_PATH_PARTS = 6

_COPY_CHUNK_BYTES = 64 * 1024

#: The only compression methods a member may use. ``zipfile`` bounds a deflate
#: read by the size asked for, but decompresses a whole BZIP2 or LZMA buffer at
#: once, so a few KB of either can expand to many MiB in memory before the byte
#: budget sees a single byte.
_ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

#: What ``zipfile`` raises for a malformed archive or a member it cannot read.
#: Each is the upload's fault, never the gateway's.
_ARCHIVE_READ_ERRORS: tuple[type[Exception], ...] = (
    zipfile.BadZipFile,  # bad header or CRC
    zlib.error,  # corrupt deflate stream
    OSError,  # a read the underlying stream refuses
    EOFError,  # member data ends before its declared size
    # Encrypted entry; also its subclass NotImplementedError, for a zip
    # version zipfile does not support.
    RuntimeError,
)

#: What constructing a ``ZipFile`` over an unreadable upload raises.
_NOT_A_ZIP_ERRORS: tuple[type[Exception], ...] = (ValueError, *_ARCHIVE_READ_ERRORS)


def _os_reason(exc: OSError) -> str:
    """The OS's reason for *exc*, without the gateway-side path it carries."""
    return exc.strerror or type(exc).__name__


def _is_archiver_metadata(parts: tuple[str, ...]) -> bool:
    """True for files a desktop archiver adds that are never part of a pack."""
    if parts and parts[0] == "__MACOSX":
        return True
    leaf = parts[-1] if parts else ""
    return leaf == ".DS_Store" or leaf.startswith("._")


def _entry_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    """The safe relative path parts of *info*, or raise ``ValueError``."""
    name = info.filename
    # ``orig_filename``, not ``filename``: on Windows ``zipfile`` rewrites a
    # backslash in ``filename`` to ``/``, so only the raw name refuses one on
    # every platform.
    if "\\" in info.orig_filename or "\x00" in info.orig_filename:
        raise ValueError(f"archive entry has an unsupported name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise ValueError(f"archive entry is an absolute path: {name!r}")
    # A drive letter or stream name in ANY component: on Windows,
    # ``dest.joinpath("pack", "D:x.json")`` discards ``dest`` entirely.
    if any(":" in part for part in path.parts):
        raise ValueError(f"archive entry has a drive or stream name: {name!r}")
    parts = tuple(p for p in path.parts if p not in ("", "."))
    if ".." in parts:
        raise ValueError(f"archive entry leaves the archive: {name!r}")
    if len(parts) > _MAX_PATH_PARTS:
        raise ValueError(f"archive entry path is too deep (maximum {_MAX_PATH_PARTS} parts)")
    return parts


def _entry_is_regular_file(info: zipfile.ZipInfo) -> bool:
    """True when *info* is a regular file; ``False`` for a directory entry.

    Raises ``ValueError`` for a symlink or any other special file. An entry
    written by a non-Unix archiver carries no mode bits, which reads as 0 and
    counts as a regular file.
    """
    if info.is_dir():
        return False
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG):
        raise ValueError(f"archive entry is not a regular file: {info.filename!r}")
    return True


def _is_existing_dir(path: Path) -> bool:
    """``path.is_dir()``, False where the name itself is unusable (ENAMETOOLONG)."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _collision(name: str) -> ValueError:
    return ValueError(f"archive has a file and a folder with the same name: {name!r}")


def _write_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path, budget: int
) -> int:
    """Copy *info* into the new file *target* and return the bytes written.

    Refuses more than *budget* bytes. Every failure to open, read or write the
    member is a ``ValueError``.
    """
    name = info.filename
    try:
        src = archive.open(info)
    except _ARCHIVE_READ_ERRORS as exc:
        raise ValueError(f"cannot read archive entry {name!r}") from exc
    with src:
        try:
            out = open(target, "xb")
        except FileExistsError as exc:
            if target.is_dir():
                raise _collision(name) from exc
            raise ValueError(f"archive lists a file twice: {name!r}") from exc
        except (IsADirectoryError, NotADirectoryError) as exc:
            raise _collision(name) from exc
        except OSError as exc:
            # Windows answers opening a folder for writing with PermissionError.
            if _is_existing_dir(target):
                raise _collision(name) from exc
            raise ValueError(f"cannot write archive entry {name!r}: {_os_reason(exc)}") from exc
        written = 0
        try:
            with out:
                while True:
                    try:
                        chunk = src.read(_COPY_CHUNK_BYTES)
                    except _ARCHIVE_READ_ERRORS as exc:
                        raise ValueError(f"cannot read archive entry {name!r}") from exc
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > budget:
                        raise ValueError("the archive unpacks to more than a theme may hold")
                    out.write(chunk)
        except OSError as exc:
            # Only the write or close of *out* reaches here: a read error has
            # already become ValueError above.
            raise ValueError(f"cannot write archive entry {name!r}: {_os_reason(exc)}") from exc
    return written


def extract_theme_zip(data: bytes, dest: Path) -> Path:
    """Unpack *data* into the empty directory *dest* and return the pack root.

    The pack root is *dest* itself when ``theme.json`` sits at the top of the
    archive, or the single top-level folder when the archive wraps the pack in
    one (what zipping a folder produces). Raises ``ValueError`` with a message
    fit for the user when the archive is unreadable or refused.
    """
    try:
        vet_zip_inventory_bytes(data, max_members=_MAX_ENTRIES)
    except ZipInventoryRejected as exc:
        if exc.reason in ("missing_eocd", "truncated_eocd", "unreadable"):
            raise ValueError("the file is not a valid .zip archive") from exc
        raise ValueError("the archive has too many entries for a theme") from exc
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except _NOT_A_ZIP_ERRORS as exc:
        raise ValueError("the file is not a valid .zip archive") from exc
    dest_root = dest.resolve()
    with archive:
        infos = archive.infolist()
        declared = 0
        planned: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for info in infos:
            parts = _entry_parts(info)
            if not parts or _is_archiver_metadata(parts):
                continue
            if not _entry_is_regular_file(info):
                continue
            if info.compress_type not in _ALLOWED_COMPRESSION:
                raise ValueError(
                    f"archive entry uses an unsupported compression method: {info.filename!r}"
                )
            declared += info.file_size
            if declared > _MAX_UNPACKED_BYTES:
                raise ValueError("the archive unpacks to more than a theme may hold")
            planned.append((info, parts))
        written = 0
        for info, parts in planned:
            name = info.filename
            target = dest.joinpath(*parts)
            try:
                inside = target.resolve().is_relative_to(dest_root)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"cannot unpack archive entry {name!r}") from exc
            if not inside:
                raise ValueError(f"archive entry leaves the archive: {name!r}")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError) as exc:
                raise _collision(name) from exc
            except OSError as exc:
                raise ValueError(
                    f"cannot create a folder for archive entry {name!r}: {_os_reason(exc)}"
                ) from exc
            written += _write_member(archive, info, target, _MAX_UNPACKED_BYTES - written)
    if (dest / _THEME_MANIFEST_NAME).is_file():
        return dest
    children = list(dest.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return dest
