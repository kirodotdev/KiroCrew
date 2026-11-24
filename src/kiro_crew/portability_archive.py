"""What a portability archive holds, read the same way by the export and the import.

The names an export leaves out (:data:`EXPORT_EXCLUDE`, :data:`EXCLUDE_DIRS`), the
memory-only member set and its manifest version, the link test for a directory entry,
the transcript naming and first-line rules a session member is judged by, and the
member-count, size and name limits an import enforces. The export and the import both
read these from here, so the archive one side writes is the archive the other accepts.
Every name is reached as ``kiro_crew.portability.<name>``; the facade binds or serves it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePath, PurePosixPath

from kiro_crew.config.paths import config_dir
from kiro_crew.history import (
    ARCHIVE_SEGMENT_DELIMITER,
    INCOGNITO_MEMORY_MODES,
    SESSIONS_DIR_NAME,
    TRANSCRIPT_HEADER_MAX_BYTES,
    _safe_key,
    memory_mode_from_header_line,
)
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME
from kiro_crew.snapshot import COMPONENTS

logger = logging.getLogger("kiro_crew.portability")

EXPORT_EXCLUDE = frozenset(
    {
        ".env",
        ".local_secret",
        "sel_hmac.key",
        "telemetry_salt",
        # NOTE: the beacon's per-install identity files (beacon_install_id /
        # beacon_last_sent) are deliberately NOT listed here. This set is matched by
        # BASENAME and `_is_excluded` runs over the workspace/, plan_memory/ and
        # skills/ trees, so an entry here would silently drop any USER file that
        # happens to share the name. They need no entry: root-level export is a
        # hard-coded allowlist (config.json, the settings documents in
        # `_SETTINGS_DOCUMENTS`, hooks.json, crons.json, notifications.jsonl,
        # folders.json, project_dir, workspace_dir, crew-teams/teams.json), so a root beacon file is
        # never selected in the first place.
        "session_map.json",
        "kiro_session_pids.txt",
        "kiro_pids.txt",
    }
)

#: The settings documents beside ``config.json`` -- everything else a dashboard Settings
#: choice is persisted in. ``config.local.json`` is the overlay that ``config set --local``
#: and ``save()`` keep overlay-owned values in, so those values exist ONLY there;
#: ``ui-prefs.json`` is the host backup of the browser-held Settings (`ui_prefs.py`);
#: ``notification_settings.json`` holds the Settings > Notifications mutes and priorities
#: (`notifications/settings.py`). The snapshot ``config`` component carries the same
#: three, and also ``mcp.json`` (the servers added on the MCP Servers page), which this
#: export does not carry.
_SETTINGS_DOCUMENTS: tuple[str, ...] = (
    "config.local.json",
    "ui-prefs.json",
    "notification_settings.json",
)

#: Folders a tool rebuilds on its own: package installs, virtual environments and tool
#: caches. A code checkout in the workspace carries them, and they can be most of an
#: export's size (one measured workspace held 4 GB of them), so an export leaves them
#: out and an import skips them, which lets an older export that carried them import too.
#: The rule covers the ``workspace/`` tree only (`_in_rebuildable_dir`): elsewhere these
#: names are user data, e.g. a memory store or a skill may be named ``venv``.
REBUILDABLE_DIRS = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".gradle",
        ".next",
        ".turbo",
        ".parcel-cache",
    }
)

EXCLUDE_DIRS = frozenset(
    {
        "snapshots",
        "outbox",
        "uploads",
        "__pycache__",
    }
)


def _in_rebuildable_dir(rest: tuple[str, ...]) -> bool:
    """True for a file inside a rebuildable folder (`REBUILDABLE_DIRS`) of the workspace.

    *rest* is the path below the data home (export) or below the archive's top directory
    (import), the tree name first. Only ``workspace/`` holds code checkouts, so only it
    is matched. Only folder names count, never the file's own name, so a file named
    ``venv`` still travels. The export and the import both ask this, so they agree.
    """
    if not rest or rest[0] != "workspace":
        return False
    return any(part in REBUILDABLE_DIRS for part in rest[1:-1])


def _mc_dir() -> Path:
    return Path(os.environ.get("KIROCREW_HOME", config_dir()))


#: The snapshot ``memory`` component, which a memory-only export carries and a
#: memory-only import applies: its two databases, the memory half of the workspace,
#: and every named store (``memory_stores/``, walked by its own filter,
#: ``_keep_store_for_export``).
MEMORY_ONLY_FILES: tuple[str, ...] = COMPONENTS["memory"].files
MEMORY_ONLY_TREES: tuple[str, ...] = tuple(
    t for t in COMPONENTS["memory"].trees if t != MEMORY_STORES_DIR_NAME
)


#: The manifest version a memory-only export writes. It sits outside the range a
#: whole-install import accepts (``1..EXPORT_MANIFEST_VERSION``), so a reader that does
#: not know memory bundles refuses one instead of applying it as a whole install, where
#: a Replace would reset every component the bundle does not carry.
MEMORY_EXPORT_MANIFEST_VERSION = 1001


def manifest_is_memory_only(manifest: object) -> bool:
    """True when an export manifest declares it carries the memory component only."""
    return isinstance(manifest, dict) and manifest.get("components") == ["memory"]


def _is_memory_member(rest: tuple[str, ...]) -> bool:
    """Whether an archive member (its path below the export prefix) is memory.

    Lexical only, on the member NAME, so a memory-only import never extracts any
    other component -- whatever else the archive carries stays in the zip.
    """
    if not rest:
        return True  # the prefix directory itself
    if rest == ("MANIFEST.json",):
        return True
    if len(rest) == 1:
        return rest[0] in MEMORY_ONLY_FILES
    if rest[0] == MEMORY_STORES_DIR_NAME:
        return True
    return any(rest[: len(t.split("/"))] == tuple(t.split("/")) for t in MEMORY_ONLY_TREES)


def _is_excluded(rel_path: PurePosixPath) -> bool:
    if rel_path.name in EXPORT_EXCLUDE:
        return True
    if rel_path.name.endswith(".pid"):
        return True
    for part in rel_path.parts:
        if part in EXCLUDE_DIRS:
            return True
    return False


#: A Windows junction is a reparse point whose tag is not the symlink tag, so
#: ``islink``/``DirEntry.is_symlink`` are False for one. The ATTRIBUTE is what
#: every kind of reparse point has in common. Absent off Windows, where the
#: concept does not exist.
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _entry_is_link(entry: os.DirEntry) -> bool:
    """True if *entry* is a symlink or, on Windows, any other reparse point.

    Answered entirely from the directory listing the kernel has ALREADY
    returned: ``FindFirstFileW`` carries ``dwFileAttributes`` and the reparse tag
    inline, so ``entry.stat(follow_symlinks=False)`` reads cached bytes rather
    than issuing a lookup -- and, decisively, never a lookup THROUGH the child.
    That is the property this walk is built on. The ordinary way to ask the same
    question, ``Path(child).is_file()`` or ``is_sensitive_path(str(child))``,
    resolves the name; if a junction there aims at ``\\\\host\\share`` that
    resolution IS an outbound SMB authentication, and no later refusal recalls
    it.

    ``DirEntry.is_symlink()`` alone is not enough and is exactly how ``rglob``
    came to descend a junction: a junction's tag is ``IO_REPARSE_TAG_MOUNT_POINT``
    rather than ``IO_REPARSE_TAG_SYMLINK``, so that method reports False for one
    while ``is_dir(follow_symlinks=False)`` reports True.

    Every reparse point is treated the same rather than only link-shaped ones,
    because that is already what the layer below refuses --
    :func:`platform_compat.pin_directory` rejects any reparse point at a
    directory name and :func:`platform_compat.open_file_no_reparse` rejects one
    at a file name. This classifies what those opens would refuse anyway: a cheap
    skip, never the enforcement. On POSIX the attribute check is skipped
    entirely; ``is_symlink()`` uses ``d_type`` and costs nothing, and asking for
    a stat there would add a real syscall for a concept the platform lacks.
    """
    if entry.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return True  # unclassifiable -> refuse to walk into it
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


#: A dashboard transcript's file name: ``<stem>.jsonl`` directly under ``sessions/``.
_TRANSCRIPT_SUFFIX = ".jsonl"

#: The longest first line a transcript header is read up to: the same bound the
#: restricted-session write gate reads, so a longer line is unknown to both.
_MAX_TRANSCRIPT_HEADER_BYTES = TRANSCRIPT_HEADER_MAX_BYTES


def _transcript_stem(name: str) -> str | None:
    """The session stem of a ``sessions/`` entry named *name*, or ``None``.

    A stem is accepted only in the spelling ``history._safe_key`` writes (word
    characters, ``-`` and ``.``), so an archive member cannot name a file this
    platform's history layer would never produce -- a ``:`` or ``\\`` in it, say.
    """
    if not name.endswith(_TRANSCRIPT_SUFFIX):
        return None
    stem = name[: -len(_TRANSCRIPT_SUFFIX)]
    if stem in ("", ".", "..") or _safe_key(stem) != stem:
        return None
    return stem


#: The stem every dashboard chat's transcript carries: a dashboard slot key is
#: ``dashboard:<slot>`` (`dashboard.chat_utils.dashboard_slot_key`), which
#: ``history._safe_key`` writes as ``dashboard_<slot>``. Channel threads, cron runs
#: and subagent runs keep transcripts under other stems and are not chats here.
_DASHBOARD_STEM_PREFIX = "dashboard_"


def _chat_stem(name: str) -> str | None:
    """The stem of the dashboard chat whose transcript is *name*, or ``None``.

    The one selection rule for which ``sessions/`` entries this feature carries, on
    export and on import alike: a valid `_transcript_stem` that starts with
    `_DASHBOARD_STEM_PREFIX` and names a slot after it.
    """
    stem = _transcript_stem(name)
    if stem is None or not stem.startswith(_DASHBOARD_STEM_PREFIX):
        return None
    if len(stem) == len(_DASHBOARD_STEM_PREFIX):
        return None
    return stem


def _transcript_header_verdict(first_line: bytes | None) -> str | None:
    """Why a transcript may not travel in an archive, or ``None`` when it may.

    Applied on both ends, export and import, so an archive can only ever carry what
    an export of this build would write. The rule is the one every derived reader
    applies to Incognito and Temporary chats: they are kept for the user's own
    History and never exported or transferred. The mode comes from
    `history.memory_mode_from_header_line`, the same parse the restricted-session
    write gate uses, and the verdict fails CLOSED -- only a header that reads
    persistent passes:

    * a metadata header with no ``memory_mode`` reads persistent (a transcript
      written before the mode existed);
    * ``memory_mode`` ``persistent`` (any case, surrounding whitespace ignored)
      passes;
    * ``incognito`` and ``temporary`` are withheld as private chats;
    * anything the parse cannot place -- no metadata header, a first line that is
      not a JSON object, a non-string or unrecognised mode -- is withheld as
      unreadable.
    """
    if first_line is None:
        return "unreadable header"
    if not first_line.strip():
        return "empty transcript"
    mode = memory_mode_from_header_line(first_line)
    if mode == "persistent":
        return None
    if mode in INCOGNITO_MEMORY_MODES:
        return "incognito or temporary chat"
    return "unreadable header"


def _read_first_line_fd(fd: int) -> bytes | None:
    """The first line behind *fd*, without its newline; ``None`` past the header cap."""
    os.lseek(fd, 0, os.SEEK_SET)
    buf = bytearray()
    while len(buf) <= _MAX_TRANSCRIPT_HEADER_BYTES:
        chunk = os.read(fd, 64 * 1024)
        if not chunk:
            return bytes(buf)
        cut = chunk.find(b"\n")
        if cut >= 0:
            buf += chunk[:cut]
            return bytes(buf) if len(buf) <= _MAX_TRANSCRIPT_HEADER_BYTES else None
        buf += chunk
    return None


def _keep_top_level_transcript(rel: PurePath) -> bool:
    """``sessions/<stem>.jsonl`` for a dashboard chat (`_chat_stem`) and nothing else.

    Lexical only, like every walk filter.
    """
    return (
        len(rel.parts) == 2
        and rel.parts[0] == SESSIONS_DIR_NAME
        and _chat_stem(rel.parts[1]) is not None
    )


def _rotation_segment_for_stem(name: str, stem: str) -> bool:
    """Match the complete stem, including when another stem contains the delimiter."""
    owner, delimiter, suffix = name.rpartition(ARCHIVE_SEGMENT_DELIMITER)
    return owner == stem and bool(delimiter) and suffix.endswith(_TRANSCRIPT_SUFFIX)


def _is_rotation_header(first_line: bytes | None) -> bool:
    """Only retained rotation history travels; discarded edits never do."""
    if first_line is None:
        return False
    try:
        header = json.loads(first_line)
    except (ValueError, RecursionError):
        return False
    return (
        isinstance(header, dict)
        and header.get("_type") == "archive"
        and header.get("reason") == "rotate"
    )


# Zip-bomb guards for import archives (CWE-409). A real personal snapshot is far
# below these ceilings; a decompression bomb (huge declared uncompressed size, or
# millions of entries) is rejected before extraction rather than filling the
# host disk.
_MAX_IMPORT_MEMBERS = 50_000
#: What every host accepts with no further check, and what an export budgets its chats
#: against. It is also the most one member may unpack to, so no file an import reads
#: whole is larger than before the checks below were added.
_MAX_IMPORT_UNCOMPRESSED = 2 * 1024**3  # 2 GiB
#: Above `_MAX_IMPORT_UNCOMPRESSED` an archive is still accepted when it stays under
#: this ceiling, unpacks to at most this many times its own size (a real export
#: compresses a few times; a zip bomb compresses hundreds or thousands of times), and
#: the data home's disk has room for it twice (the staged copy and the installed one)
#: plus a reserve.
_MAX_IMPORT_UNCOMPRESSED_CEILING = 64 * 1024**3
_MAX_IMPORT_COMPRESSION_RATIO = 100
_IMPORT_DISK_RESERVE = 1024**3


def _skipped_on_import(parts: tuple[str, ...]) -> bool:
    """True for a member the import skips: `_in_rebuildable_dir` below the top directory.

    *parts* is the member name split on ``/``, the archive's top directory first.
    """
    return _in_rebuildable_dir(parts[1:])


def _gb(nbytes: int) -> str:
    return f"{nbytes / 1e9:.2f} GB"


def _largest_folders(infos: list[zipfile.ZipInfo], count: int = 3) -> str:
    """The *count* biggest folders (two levels deep) by unpacked size, as text."""
    sizes: dict[str, int] = {}
    for info in infos:
        parts = PurePosixPath(info.filename).parts[1:]
        if len(parts) > 2:
            key = "/".join(parts[:2])
        elif parts:
            key = parts[0]
        else:
            key = ""
        sizes[key] = sizes.get(key, 0) + info.file_size
    top = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:count]
    return ", ".join(f"{name} ({_gb(size)})" for name, size in top if name)


_SHRINK_HINT = (
    "Make a new export in Settings > Import / Export > Back up & restore configuration: "
    "it leaves out folders that tools rebuild (node_modules, .venv, caches). If it is still "
    "too big, move the biggest folders out of the workspace folder before you export."
)


def import_size_problem(infos: list[zipfile.ZipInfo], archive_bytes: int) -> str | None:
    """Why an archive is too big to import here, in words a user can act on, or None.

    *infos* is the archive's member list and *archive_bytes* the archive file's real
    size on disk. Members in rebuildable folders are not counted, since the import skips
    them. Declared sizes can be trusted as upper bounds: ``zipfile`` stops a member at
    its declared size and fails on a mismatch.
    """
    kept = [i for i in infos if not _skipped_on_import(PurePosixPath(i.filename).parts)]
    for info in kept:
        if info.file_size > _MAX_IMPORT_UNCOMPRESSED:
            return (
                f"Rejected: {info.filename} unpacks to {_gb(info.file_size)}, more than the "
                f"{_gb(_MAX_IMPORT_UNCOMPRESSED)} one file may hold. {_SHRINK_HINT}"
            )
    total = sum(i.file_size for i in kept)
    if total <= _MAX_IMPORT_UNCOMPRESSED:
        return None
    if total > _MAX_IMPORT_COMPRESSION_RATIO * max(archive_bytes, 1):
        return (
            f"Rejected: a {_gb(archive_bytes)} archive that claims to unpack to "
            f"{_gb(total)} (more than {_MAX_IMPORT_COMPRESSION_RATIO} times its size) is "
            "not a Kiro Crew export (possible zip bomb)"
        )
    biggest = _largest_folders(kept)
    if total > _MAX_IMPORT_UNCOMPRESSED_CEILING:
        return (
            f"Rejected: this backup unpacks to {_gb(total)}, more than the "
            f"{_gb(_MAX_IMPORT_UNCOMPRESSED_CEILING)} an import accepts. Biggest folders: "
            f"{biggest}. {_SHRINK_HINT}"
        )
    home = _mc_dir()
    probe = home if home.is_dir() else home.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        free = 0
    needed = 2 * total + _IMPORT_DISK_RESERVE
    if free < needed:
        return (
            f"Rejected: this backup unpacks to {_gb(total)} and the import needs "
            f"{_gb(needed)} of free disk space, but only {_gb(free)} is free. Free up disk "
            f"space and try again. Biggest folders: {biggest}. {_SHRINK_HINT}"
        )
    return None


def _is_link_entry(info: zipfile.ZipInfo) -> bool:
    """True when a zip member declares itself a symlink or hardlink.

    CPython's ``ZipFile.extract`` does NOT honor S_IFLNK -- it writes the link
    target as ordinary file content -- so a link member cannot currently redirect
    a later write outside the extraction root. This guard exists because that is a
    property of the extraction backend rather than of the archive: swapping in
    ``shutil.unpack_archive``, an external ``unzip``, or a future stdlib that
    honors the mode bit would silently turn a link member into a real symlink and
    make the escape reachable (CWE-22 via CWE-59). Rejecting these members keeps
    the guarantee at the archive boundary, where it does not depend on which
    extractor runs. Legitimate archives carry none: the export side skips symlinks.
    """
    mode = info.external_attr >> 16
    return bool(mode) and stat.S_ISLNK(mode)


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def _unsafe_member_name(name: str, *, backslash_separates: bool | None = None) -> bool:
    """True for a member name an extractor could place somewhere other than it reads.

    Covers traversal and absolute names, and the Windows forms: a drive prefix
    (``C:/x``) that ``zipfile`` strips on extraction, and, where the host treats a
    backslash as a separator, a backslash-spelled traversal or drive. A stripped
    drive moves the member up one level, out of the archive prefix, so a lexical
    filter on the name (the memory-only filter, the one-top-directory check) would
    describe a different place from where it lands. On POSIX a backslash is an
    ordinary filename character that the export itself can write, so it is not
    refused there.
    """
    if backslash_separates is None:
        backslash_separates = os.sep == "\\" or os.altsep == "\\"
    if backslash_separates:
        name = name.replace("\\", "/")
    if name.startswith("/"):
        return True
    parts = PurePosixPath(name).parts
    if ".." in parts:
        return True
    return bool(parts) and bool(_DRIVE_PREFIX.match(parts[0]))


#: The largest archive settings document an import will read. The archive itself may
#: hold 2 GiB, and these documents are parsed before Merge decides to keep this
#: install's copy, so an unbounded read could exhaust the gateway's memory on a file it
#: then throws away. Real settings documents are a few KiB.
_MAX_SETTINGS_DOCUMENT_BYTES = 8 * 1024 * 1024
