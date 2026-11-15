"""Move the absolute paths an import archive carries from its home to this one.

An export from one computer names folders under that computer's home: a chat's
project (``/home/<user>/app``), the saved workspace root, the dashboard's default
project. Installed as is on a computer with another home (``/Users/<user>``), the
first session that opens one of them creates that folder, and on macOS the
volume root is read-only, so it fails with ``[Errno 30] Read-only file system``.

:func:`rewrite_staged_paths` runs on the staged extraction, before either import
mode copies anything. A path under the archive's home moves to the same place
under this home. Any other path stays only when it is a directory on this
computer; otherwise it is dropped, so the chat opens in the default workspace
instead of failing, and the summary counts it. Nothing here writes outside the
staged tree: a moved path always lands under this home, and the folders it names
are created later, if at all, by the code that opens them.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from kiro_crew.atomic_write import atomic_write
from kiro_crew.bounded_read import bounded_read
from kiro_crew.history_derivation import TRANSCRIPT_HEADER_MAX_BYTES
from kiro_crew.user_json import exceeds_nesting, strip_utf8_bom

#: The largest ``config.json`` or manifest this reads, as the import's settings cap.
_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

#: The longest home a manifest may name; a real one is far shorter.
_MAX_HOME_CHARS = 4096

#: The data-home files that each hold one saved folder path.
_PATH_FILES = ("workspace_dir", "project_dir")
_CONFIG_FILES = ("config.json", "config.local.json")


def _posix_home(value: object) -> str | None:
    """*value* as an absolute POSIX home other than the root, or ``None``.

    shortcut: POSIX homes only, so an archive from Windows is left as it is;
    upgrade when a Windows-to-POSIX move is reported.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_HOME_CHARS:
        return None
    if "\x00" in value or not value.startswith("/"):
        return None
    parts = PurePosixPath(value).parts
    if len(parts) < 2 or ".." in parts:
        return None
    return str(PurePosixPath(*parts))


def map_home_path(value: str, old_home: str, new_home: str) -> str | None:
    """*value* moved from under *old_home* to under *new_home*, or ``None``.

    ``None`` when *value* is not *old_home* or a path below it. The part below the
    home is carried over only when it has no ``..``, so the result always stays
    under *new_home*.
    """
    if not value.startswith("/"):
        return None
    parts = PurePosixPath(value).parts
    old_parts = PurePosixPath(old_home).parts
    if parts[: len(old_parts)] != old_parts:
        return None
    rest = parts[len(old_parts) :]
    if ".." in rest:
        return None
    return str(PurePosixPath(new_home, *rest))


def this_home() -> str | None:
    """This computer's home as :func:`map_home_path` takes it, or ``None`` (Windows)."""
    return _posix_home(str(Path.home()))


def archive_home(staged: Path) -> tuple[str, ...] | None:
    """The archive's distinct home spellings, longest first, or ``None``.

    A current export names its lexical ``home`` and resolved ``home_real`` in the
    manifest. An older one does not, so a single home is read from the archive's
    own paths: the prefix that ends at the exporting user's name (the manifest's
    ``user``), the most common one winning.
    """
    manifest = _read_json_object(staged / "MANIFEST.json")
    if manifest is None:
        return None
    named = {home for key in ("home", "home_real") if (home := _posix_home(manifest.get(key)))}
    if named:
        return tuple(sorted(named, key=len, reverse=True))
    user = manifest.get("user")
    if not isinstance(user, str) or not user or user == "unknown":
        return None
    seen: Counter[str] = Counter()
    for path in _archive_paths(staged):
        parts = PurePosixPath(path).parts
        if user in parts[1:]:
            home = _posix_home(str(PurePosixPath(*parts[: parts.index(user, 1) + 1])))
            if home is not None:
                seen[home] += 1
    if not seen:
        return None
    guessed = seen.most_common(1)[0][0]
    # A real project prefix on this computer is not evidence of a different home.
    return None if os.path.exists(guessed) else (guessed,)


def _archive_paths(staged: Path) -> list[str]:
    """Every absolute path the rewrite would look at, for :func:`archive_home`."""
    found = [p for name in _PATH_FILES if (p := _read_path_file(staged / name))]
    for name in _CONFIG_FILES:
        config = _read_json_object(staged / name)
        dashboard = config.get("dashboard") if config else None
        if isinstance(dashboard, dict) and isinstance(dashboard.get("default_project"), str):
            found.append(dashboard["default_project"])
    for transcript in _transcripts(staged):
        header = _read_header(transcript)
        if header is not None and isinstance(header[0].get("project"), str):
            found.append(header[0]["project"])
    return [p for p in found if p.startswith("/")]


def _new_value(value: str, old_homes: tuple[str, ...], new_home: str) -> str | None:
    """What a saved folder path becomes here: moved, kept, or dropped (``None``)."""
    for old_home in old_homes:
        moved = map_home_path(value, old_home, new_home)
        if moved is not None:
            return moved
    return value if os.path.isdir(value) else None


@dataclass(frozen=True)
class PathRewrite:
    """What :func:`rewrite_staged_paths` changed in the staged extraction."""

    moved: int = 0
    """Paths moved under the new home."""
    dropped: int = 0
    """Paths dropped in all, chat projects included."""
    cleared_chats: frozenset[str] = frozenset()
    """File names (``dashboard_x.jsonl``) of the transcripts whose project was dropped.

    Names, not a count: the import installs only some staged chats, and only those
    may be reported as opening in the default workspace."""


def rewrite_staged_paths(staged: Path, old_homes: tuple[str, ...], new_home: str) -> PathRewrite:
    """Rewrite the saved folder paths in the staged extraction *staged*.

    Returns how many paths were moved under *new_home*, how many were dropped in
    all, and which transcripts lost their project. Only absolute paths are judged;
    a saved path file first loses its outer quotes, as its workspace reader drops
    them, and a ``~`` path already follows the home it is read on.
    Same home (either spelling): nothing changes. *old_homes* comes
    from :func:`archive_home`, longest first so nested aliases match correctly.
    """
    counts = {"moved": 0, "dropped": 0}
    cleared: set[str] = set()
    here = os.path.realpath(new_home)
    if all(os.path.realpath(home) == here for home in old_homes):
        return PathRewrite()

    def judge(value: str) -> str | None:
        new = _new_value(value, old_homes, new_home)
        if new is None:
            counts["dropped"] += 1
        elif new != value:
            counts["moved"] += 1
        return new

    for name in _PATH_FILES:
        path_file = staged / name
        value = _read_path_file(path_file)
        if not value.startswith("/"):
            continue
        new = judge(value)
        if new is None:
            path_file.unlink()
        elif new != value:
            atomic_write(path_file, new + "\n")

    for name in _CONFIG_FILES:
        _rewrite_config(staged / name, judge)

    for transcript in _transcripts(staged):
        header = _read_header(transcript)
        if header is None:
            continue
        fields, size = header
        project = fields.get("project")
        if not isinstance(project, str) or not project.startswith("/"):
            continue
        new = judge(project)
        if new == project:
            continue
        if new is None:
            cleared.add(transcript.name)
            del fields["project"]
        else:
            fields["project"] = new
        _replace_header(transcript, fields, size)
    return PathRewrite(counts["moved"], counts["dropped"], frozenset(cleared))


def _open_regular(path: Path) -> BinaryIO:
    """Open a staged regular file without following a swapped-in leaf symlink."""
    # Nonblocking open lets fstat reject a FIFO without waiting for its writer.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("staged input is not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _read_path_file(path: Path) -> str:
    """The one path saved in *path*, or ``""`` (read as ``workspace_root`` reads it)."""
    if not path.is_file() or path.is_symlink():
        return ""
    try:
        with _open_regular(path) as fh:
            # One folder path, with room for UTF-8, quotes and surrounding whitespace.
            cap = _MAX_HOME_CHARS * 4 + 16
            raw = bounded_read(fh, cap + 1)
            if len(raw) > cap:
                return ""
            value = strip_utf8_bom(raw.decode("utf-8")).strip()
    except (OSError, UnicodeDecodeError):
        return ""
    # One symmetric outer quote pair is dropped, as ``normalize_workspace_path`` drops
    # it; ``~`` is left as is, since it already follows the home it is read on.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _read_json_object(path: Path) -> dict | None:
    """*path* as a JSON object, or ``None`` when it is missing, too big or not one."""
    if not path.is_file() or path.is_symlink():
        return None
    try:
        with _open_regular(path) as fh:
            if os.fstat(fh.fileno()).st_size > _MAX_DOCUMENT_BYTES:
                return None
            doc = json.loads(strip_utf8_bom(fh.read().decode("utf-8")))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or exceeds_nesting(doc):
        return None
    return doc


def _rewrite_config(path: Path, judge: Callable[[str], str | None]) -> None:
    """Rewrite ``dashboard.default_project`` in a staged config document.

    A document this cannot read is left for the import's own settings check.
    """
    config = _read_json_object(path)
    dashboard = config.get("dashboard") if config else None
    if config is None or not isinstance(dashboard, dict):
        return
    value = dashboard.get("default_project")
    if not isinstance(value, str) or not value.startswith("/"):
        return
    new = judge(value)
    if new == value:
        return
    if new is None:
        del dashboard["default_project"]
    else:
        dashboard["default_project"] = new
    atomic_write(path, json.dumps(config, indent=2) + "\n")


def _transcripts(staged: Path) -> list[Path]:
    """The staged chats' transcript files (``sessions/<stem>.jsonl``)."""
    sessions = staged / "sessions"
    if not sessions.is_dir() or sessions.is_symlink():
        return []
    return sorted(p for p in sessions.glob("*.jsonl") if p.is_file() and not p.is_symlink())


def _read_header(transcript: Path) -> tuple[dict, int] | None:
    """The transcript's metadata header and its size in bytes, or ``None``."""
    try:
        with _open_regular(transcript) as fh:
            line = fh.readline(TRANSCRIPT_HEADER_MAX_BYTES + 1)
    except OSError:
        return None
    if len(line) > TRANSCRIPT_HEADER_MAX_BYTES or not line.endswith(b"\n"):
        return None
    try:
        fields = json.loads(line)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(fields, dict) or fields.get("_type") != "metadata":
        return None
    return fields, len(line)


def _replace_header(transcript: Path, fields: dict, old_size: int) -> None:
    """Write *fields* as the transcript's first line, keeping the rest and its times.

    The staged file's times are the chat's own (the import copies them on), so they
    are put back after the swap.
    """
    try:
        src = _open_regular(transcript)
    except OSError:
        return
    with src:
        st = os.fstat(src.fileno())
        fd, tmp = tempfile.mkstemp(dir=transcript.parent, prefix=".rewrite-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(json.dumps(fields).encode("utf-8") + b"\n")
                src.seek(old_size)
                while chunk := src.read(1 << 20):
                    out.write(chunk)
            src.close()  # Windows cannot replace a file while its read handle is open.
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, transcript)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
