"""The fingerprint that pins the operator's Strands project (``agent.strands_project``).

The project's ``agent.py`` is imported inside the Strands worker, which also holds the
provider keys from ``agent.strands_env``. The ask-before-every-tool gate sees tool
calls, not module code, so whatever the project's files say runs ungated, with the
keys, on the next launch. A project kept where the agent can write (an approved edit,
another chat's shell, a prompt injection) could therefore be changed into code the
operator never saw.

So the operator confirms one version of the project, and Kiro Crew runs only that
version. Confirming records this fingerprint as ``agent.strands_project_sha256`` in
``config.json``, which the agent's file tools may not write and the OS sandbox mounts
read-only for every agent shell (unless the operator turned the sandbox off). Each launch recomputes it before any project code is
imported and before any key is handed over, here in the gateway and again inside the
worker right before the import (``kiro_crew_strands_gate._project_fingerprint``,
the same algorithm, kept equal by a test). A mismatch, or no recorded fingerprint, refuses
the session; nothing ever records a new one on its own. Inside the worker every import
that lands in the project folder, for the whole session, runs only a confirmed ``.py``
file from the bytes it checked (``kiro_crew_strands_gate._ConfirmedProjectFinder``).

What the fingerprint covers: every regular file under the folder that holds the
project's ``agent.py`` (the folder named, or the named file's own folder), found
recursively, as sorted relative POSIX paths and their bytes. ``__pycache__`` folders
are left out because no project file is ever run from bytecode. A symlink, a junction, any other
non-regular entry, an unreadable file, or a folder past :data:`MAX_FILES` /
:data:`MAX_BYTES` refuses rather than being skipped. Code the project imports from
installed packages outside the folder is NOT covered.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from kiro_crew import platform_compat

__all__ = [
    "MAX_BYTES",
    "MAX_FILES",
    "PIN_SETTING",
    "ProjectPinError",
    "project_fingerprint",
    "project_pin_issue",
]

#: The setting that holds the confirmed fingerprint.
PIN_SETTING = "agent.strands_project_sha256"

#: Bounds on what one fingerprint reads. A folder past either is refused, not trimmed.
MAX_FILES = 5000
MAX_BYTES = 64 * 1024 * 1024

#: Prefixes the digest so a fingerprint of another shape can never collide with this one.
_DOMAIN = b"kirocrew-strands-project-v1\0"
_SKIPPED_DIR = "__pycache__"
_CHUNK = 1024 * 1024


class ProjectPinError(ValueError):
    """The project folder cannot be fingerprinted; the message says why, for the operator."""


def _collect(root: Path) -> list[tuple[str, Path]]:
    """Every regular file under *root* as ``(relative POSIX path, path)``, or a refusal."""
    found: list[tuple[str, Path]] = []
    pending: list[tuple[Path, str]] = [(root, "")]
    while pending:
        folder, prefix = pending.pop()
        try:
            with os.scandir(folder) as entries:
                listed = list(entries)
        except OSError as exc:
            raise ProjectPinError(f"cannot list {folder} ({exc.strerror or exc})") from None
        for entry in listed:
            rel = f"{prefix}{entry.name}"
            path = Path(entry.path)
            if platform_compat.is_link_or_junction(path):
                raise ProjectPinError(
                    f"{rel} is a link; Kiro Crew does not follow links out of the project "
                    "folder. Replace it with the real file or folder"
                )
            try:
                mode = os.lstat(path).st_mode
            except OSError as exc:
                raise ProjectPinError(f"cannot read {rel} ({exc.strerror or exc})") from None
            if stat.S_ISDIR(mode):
                if entry.name != _SKIPPED_DIR:
                    pending.append((path, f"{rel}/"))
            elif stat.S_ISREG(mode):
                found.append((rel, path))
                if len(found) > MAX_FILES:
                    raise ProjectPinError(
                        f"the project folder holds more than {MAX_FILES} files. Move what "
                        "the project does not need (a virtualenv, data, a git checkout's "
                        "history) out of the folder"
                    )
            else:
                raise ProjectPinError(f"{rel} is not a regular file or folder")
    return found


def project_fingerprint(entry: str | os.PathLike[str]) -> tuple[str, int]:
    """``(sha256 hex, file count)`` over the folder holding the project's *entry* file.

    Raises :exc:`ProjectPinError` for anything that cannot be fingerprinted exactly.
    Blocking (walks and reads the folder).
    """
    root = Path(entry).parent
    files = sorted(_collect(root))
    digest = hashlib.sha256(_DOMAIN)
    total = 0
    for rel, path in files:
        try:
            fd = platform_compat.open_file_no_reparse(path, nonblocking=True, links_only=True)
        except OSError as exc:
            raise ProjectPinError(f"cannot read {rel} ({exc.strerror or exc})") from None
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ProjectPinError(f"{rel} is not a regular file")
            total += info.st_size
            if total > MAX_BYTES:
                raise ProjectPinError(
                    f"the project folder holds more than {MAX_BYTES // (1024 * 1024)} MB. "
                    "Move what the project does not need out of the folder"
                )
            name = rel.encode("utf-8", "surrogateescape")
            digest.update(len(name).to_bytes(8, "big") + name)
            digest.update(info.st_size.to_bytes(8, "big"))
            read = 0
            while read <= info.st_size and (chunk := handle.read(_CHUNK)):
                read += len(chunk)
                digest.update(chunk)
            if read != info.st_size:
                raise ProjectPinError(f"{rel} changed while Kiro Crew read it")
    return digest.hexdigest(), len(files)


def project_pin_issue(entry: str, pinned: str) -> str:
    """Why the project at *entry* may not launch under the confirmed *pinned* value, or ``""``.

    The answer is a whole sentence for the operator, ending with how to confirm the
    project. It never records anything. Blocking.
    """
    folder = str(Path(entry).parent)
    try:
        current, count = project_fingerprint(entry)
    except ProjectPinError as exc:
        return (
            f"Kiro Crew cannot fingerprint the Strands project in {folder}: {exc}. "
            "Its code runs with your provider keys, so Kiro Crew runs only a project "
            "folder it can check file by file."
        )
    if pinned.strip().lower() == current:
        return ""
    state = "was never confirmed" if not pinned.strip() else "changed since you confirmed it"
    return (
        f"The Strands project in {folder} {state}. Its code runs with your provider "
        "keys and outside the tool gate, so Kiro Crew runs only the version you "
        f"confirmed. Review the {count} files in that folder, then confirm this "
        f"version: kirocrew config set {PIN_SETTING} {current}"
        " (or set the same value in config.json). Start a new chat after that."
    )
