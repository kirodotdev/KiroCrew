"""What an import checks before its archive's state is trusted.

:func:`validate_import_zip` reads the archive's inventory and its one manifest under the
member, size and name limits; the staged cron store is vetted again and published
(:func:`_sanitize_imported_crons`); and host-local state is stripped from the staged
memory stores (:func:`_strip_host_local_store_state`).
"""

from __future__ import annotations

import json
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.mcp_cron import _log_cron_denial, _vet_shell_command
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME, is_host_local_store_state
from kiro_crew.portability_archive import (
    _MAX_IMPORT_MEMBERS,
    _MAX_IMPORT_UNCOMPRESSED,
    _MAX_SETTINGS_DOCUMENT_BYTES,
    MEMORY_EXPORT_MANIFEST_VERSION,
    _is_link_entry,
    _unsafe_member_name,
    manifest_is_memory_only,
)
from kiro_crew.snapshot import EXPORT_MANIFEST_VERSION
from kiro_crew.zip_vet import ZipInventoryRejected, vet_zip_inventory


def _read_manifest_bytes(zf: zipfile.ZipFile, member: str | zipfile.ZipInfo) -> bytes:
    """A manifest member's bytes, refusing one larger than the manifest budget.

    The budget is ``_MAX_SETTINGS_DOCUMENT_BYTES``, the same one the export sizes
    its manifest to (session times included), so every backup this product writes
    reads back. Checked before anything is inflated, so an oversized member is
    never decoded. Pass the ``ZipInfo`` when iterating entries: a name resolves to
    the LAST entry of that name, so reading duplicates by name reads one entry many
    times.
    """
    info = member if isinstance(member, zipfile.ZipInfo) else zf.getinfo(member)
    if info.file_size > _MAX_SETTINGS_DOCUMENT_BYTES:
        raise ValueError(f"Manifest member too large: {info.filename}")
    return zf.read(info)


def _top_manifest(infos: list[zipfile.ZipInfo], *, required: bool = True) -> zipfile.ZipInfo | None:
    """The archive's one manifest, at ``<prefix>/MANIFEST.json``.

    Only that placement is the manifest: a ``MANIFEST.json`` deeper in the tree is
    ordinary content (a workspace or skill file can carry that name), so it never
    decides what an import applies. Two at the top, under one prefix or two, are
    refused, since then no single manifest describes the archive. With none,
    ``required`` decides between refusing and returning ``None``.
    """
    tops = [
        info
        for info in infos
        if len(PurePosixPath(info.filename).parts) == 2
        and PurePosixPath(info.filename).name == "MANIFEST.json"
    ]
    if len(tops) > 1:
        raise ValueError("More than one top-level MANIFEST.json in archive")
    if not tops:
        if required:
            raise ValueError("No MANIFEST.json found in archive")
        return None
    return tops[0]


def validate_import_zip(zip_path: Path) -> tuple[bool, str, dict]:
    """Validate a zip file for import.

    Returns (ok, error_message, manifest_dict).
    """
    try:
        vet_zip_inventory(zip_path, max_members=_MAX_IMPORT_MEMBERS)
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            names = zf.namelist()

            # Check for path traversal
            for name in names:
                if _unsafe_member_name(name):
                    return False, f"Rejected path traversal: {name}", {}

            # Zip-bomb guard: bound entry count and total uncompressed size.
            infos = zf.infolist()
            if len(infos) > _MAX_IMPORT_MEMBERS:
                return (
                    False,
                    f"Rejected: archive has too many entries ({len(infos)} > {_MAX_IMPORT_MEMBERS})",
                    {},
                )
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > _MAX_IMPORT_UNCOMPRESSED:
                return (
                    False,
                    (
                        f"Rejected: uncompressed size {total_uncompressed} exceeds cap "
                        f"{_MAX_IMPORT_UNCOMPRESSED} (possible zip bomb)"
                    ),
                    {},
                )

            # Link members can redirect a later write outside the extraction root
            # even when every name passes the traversal check above.
            for info in infos:
                if _is_link_entry(info):
                    return False, f"Rejected link entry: {info.filename}", {}

            # Find manifest
            try:
                top = _top_manifest(zf.infolist())
                if top is None:  # required=True raises instead; this narrows the type
                    return False, "No MANIFEST.json found in archive", {}
                manifest_bytes = _read_manifest_bytes(zf, top)
            except ValueError as exc:
                return False, f"Rejected: {exc}", {}
            manifest_data = json.loads(manifest_bytes)
            version = manifest_data.get("version")
            memory_bundle = version == MEMORY_EXPORT_MANIFEST_VERSION and manifest_is_memory_only(
                manifest_data
            )
            if not memory_bundle and (
                not isinstance(version, int) or not 1 <= version <= EXPORT_MANIFEST_VERSION
            ):
                return False, f"Unsupported manifest version: {version}", {}

            return True, "", manifest_data
    except ZipInventoryRejected as exc:
        if exc.reason == "too_many_members":
            return False, f"Rejected: archive has too many entries ({exc})", {}
        if exc.reason in {"cdir_too_large", "zip64_saturated"}:
            return False, f"Rejected: archive inventory exceeds cap ({exc}; possible zip bomb)", {}
        return False, "Invalid zip file", {}
    except zipfile.BadZipFile:
        return False, "Invalid zip file", {}
    except (json.JSONDecodeError, KeyError) as e:
        return False, f"Invalid manifest: {e}", {}


#: Stands in for a job name when the whole store had to be replaced, so the
#: import summary can say something happened without inventing a name.
_UNREADABLE_STORE = "<the whole cron store was unreadable>"


def _sanitize_imported_crons(crons_path: Path) -> tuple[list[str], list[str]]:
    """Make an imported cron store safe to load and safe to run.

    Returns ``(dropped, paused)`` — two lists, because they are two different
    outcomes and a caller that conflates them tells the user the wrong thing. A
    dropped job is gone; a paused one is fully restored and simply waiting to be
    switched on. Replaces *crons_path* atomically when it changes anything; a
    missing file, or a directory at that name, is left alone.

    The store is read and written as UTF-8, never through the locale codepage.
    Cron job names are operator-authored text and routinely non-ASCII — the same
    reason ``snapshot._merge_crons``, which merges this very file a few lines
    later in ``apply_import_zip``, pins ``encoding="utf-8"`` on both ends. A bare
    ``read_text()`` here decodes the archive's UTF-8 with the host codepage, and
    both outcomes are wrong: a codepage that cannot decode the bytes raises
    ``UnicodeDecodeError``, which IS a ``ValueError`` and therefore lands in the
    recovery arm below — replacing a perfectly good backup with an empty store
    and reporting it as unreadable — while a codepage that decodes most bytes
    (cp1252) yields mojibake that the rewrite below then persists to disk.

    Three rules, each closing a different way an archive can act on the host:

    1. A job that is not an object, or whose ``schedule`` is not one, is DROPPED.
       ``CronService._load`` skips such a record with a warning and it is then
       silently dropped from the store on the next write — dead weight with an
       invisible deadline. Dropping it at import, with the drop REPORTED to the
       user, is the honest version of the same outcome.

    2. A ``command`` is vetted with ``mcp_cron._vet_shell_command``, so it is
       judged exactly as the same command would be at ``cron_add`` (deny-list,
       sensitive-path, credential-path and exfiltration checks). A failure DROPS
       the job, and so does the vet itself raising — an unverifiable command must
       not be scheduled.

    3. A job that survives with a ``command``, and any job naming a ``script``,
       is imported DISABLED (``user_paused``) rather than live, and reported as
       PAUSED, not rejected. The vet bounds what a command may do, not whether the
       user asked for THIS command on THIS machine, and a ``script`` cannot be
       vetted at all: the export never carries the ``crons/`` directory, so the
       name resolves against whatever the target already has there. Both become an
       ambush if they start running on their own. Disabling keeps the restore — the
       jobs, their schedules and their history are all still there — while making
       the first run an explicit human action. Message-only jobs are untouched:
       they prompt an agent, they do not execute anything on the host.
    """
    # The store is resolved ONCE for the read and published by an atomic replace
    # in the same pinned directory. A by-name read followed by a by-name rewrite is
    # two resolutions: a link or hardlink alias swapped in between would have the
    # rewrite follow it and truncate whatever it names. A rename replaces the
    # directory entry and never follows it, so what lands is what was judged.
    dir_fd: int | None = None
    try:
        if pinned_fs.supports_pinned_walk():
            dir_fd = pinned_fs.pin_parent(
                os.path.realpath(crons_path.parent), what="imported cron store", refusal=OSError
            )
        try:
            text = _read_staged_cron_store(crons_path, dir_fd)
        except FileNotFoundError:
            return [], []
        except IsADirectoryError:
            # Not a file at all: left alone exactly as the by-name `is_file()` did.
            return [], []
        return _sanitize_cron_text(text, crons_path, dir_fd)
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def _read_staged_cron_store(crons_path: Path, dir_fd: int | None) -> str | None:
    """Open the staged store once, judge the descriptor, and read through it.

    Returns ``None`` when the name holds something that is not one regular file
    (a link, a FIFO, a hardlink alias): that is unreadable as a store and is
    replaced, never followed. ``O_NONBLOCK`` keeps a FIFO from hanging the
    import. Raises ``FileNotFoundError`` for a missing store and
    ``IsADirectoryError`` for a directory, which the caller leaves alone.
    """
    try:
        if dir_fd is not None:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            fd = os.open(crons_path.name, flags, dir_fd=dir_fd)
        else:
            fd = platform_compat.open_file_no_reparse(crons_path, nonblocking=True)
    except (FileNotFoundError, IsADirectoryError):
        raise
    except OSError:
        return None
    try:
        seen = os.fstat(fd)
        if stat.S_ISDIR(seen.st_mode):
            raise IsADirectoryError(str(crons_path))
        if not stat.S_ISREG(seen.st_mode) or seen.st_nlink != 1:
            return None
        chunks = []
        try:
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
            # UTF-8, never the locale codepage: see _sanitize_imported_crons.
            return b"".join(chunks).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    finally:
        os.close(fd)


def _publish_staged_cron_store(crons_path: Path, dir_fd: int | None, data: object) -> None:
    """Replace the staged store's directory entry with *data*, never writing through it."""
    from kiro_crew.atomic_write import pinned_parent_replace_supported

    pinned = dir_fd if dir_fd is not None and pinned_parent_replace_supported() else None
    atomic_write(crons_path, json.dumps(data, indent=2), parent_dir_fd=pinned)


def _sanitize_cron_text(
    text: str | None, crons_path: Path, dir_fd: int | None
) -> tuple[list[str], list[str]]:
    """Apply the three rules of :func:`_sanitize_imported_crons` to the bytes read once."""
    try:
        if text is None:
            raise ValueError("the staged cron store is not one regular file")
        data = json.loads(text)
    except ValueError:
        # Unparseable bytes are not installable as a cron store either, but they
        # are also not something this function can reason about — an empty store
        # is the only safe thing to hand the loader.
        _publish_staged_cron_store(crons_path, dir_fd, {"jobs": []})
        return [_UNREADABLE_STORE], []
    # A store whose top level is not an object, or whose `jobs` is not a list, is
    # REPLACED rather than left alone. `CronService._load` treats such a document
    # as unsalvageable (empty registry, warning) — replacing it here means the
    # user is TOLD the store was unreadable at import time, instead of the
    # gateway silently starting with an empty schedule later.
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        _publish_staged_cron_store(crons_path, dir_fd, {"jobs": []})
        return [_UNREADABLE_STORE], []
    jobs = data["jobs"]

    kept: list = []
    dropped: list[str] = []
    paused: list[str] = []
    changed = False

    def _name_of(job: object) -> str:
        name = job.get("name") if isinstance(job, dict) else None
        return str(name) if name else "<unnamed>"

    for job in jobs:
        # Rule 1: a shape unfit for the store must not survive the import. This
        # predicate is deliberately STRICTER than the loader's (it also demands
        # str-typed id/name/message, which `_job_from_record` would accept
        # untyped): the loader skips a bad record with a warning and the next
        # write drops it for good, so importing anything questionable only
        # manufactures dead weight with an invisible deadline. Dropping it here,
        # reported in the import summary, tells the user it happened.
        if (
            not isinstance(job, dict)
            or not all(isinstance(job.get(f), str) for f in ("id", "name", "message"))
            or not isinstance(job.get("schedule"), dict)
            or not isinstance(job["schedule"].get("kind"), str)
        ):
            dropped.append(_name_of(job))
            changed = True
            continue

        command = job.get("command", "")
        script = job.get("script", "")

        # Rule 2: the command is judged exactly as `cron_add` would judge it.
        if command:
            try:
                reason = _vet_shell_command(command)
            except Exception:  # noqa: BLE001 — unverifiable command must fail closed
                reason = "command could not be verified"
            if reason is not None:
                dropped.append(_name_of(job))
                changed = True
                # Same audit obligation as a `cron_add` denial: the dropped
                # command never reaches the ACP permission/hook flow, so this is
                # the only place the denial can be recorded. Named for where it
                # happened, so an import-time drop is not read as an attempted
                # `cron_add`.
                _log_cron_denial("settings_import", reason)
                continue

        # Rule 3: anything that EXECUTES arrives paused, awaiting a human.
        if (command or script) and not job.get("user_paused", False):
            job["user_paused"] = True
            job["enabled"] = False
            changed = True
            paused.append(_name_of(job))
            _log_cron_denial(
                "settings_import",
                "Error: an imported job that runs a command or script is "
                "restored paused until it is enabled by hand",
            )
        kept.append(job)

    if changed:
        data["jobs"] = kept
        _publish_staged_cron_store(crons_path, dir_fd, data)
    return dropped, paused


def _strip_host_local_store_state(snap: Path) -> None:
    """Remove from the extracted archive what no export ever writes under ``memory_stores/``.

    The export's own filter (`_keep_store_for_export`) keeps these out on the way out; this
    is the same predicate applied on the way in, so an import is not the one direction in
    which a hand-built archive can plant them. Pruned at the first matching component, so a
    whole ``.execution-logs/`` or ``<store>/backups/`` goes as one removal.

    An absent or non-directory ``memory_stores`` is a no-op, answered by the pin chain
    below rather than by a by-name ``is_dir()`` probe ahead of it -- that probe would be
    the screen-then-act shape this traversal exists to remove.
    """

    # Every directory is PINNED before its entries are judged, and each entry is acted
    # on through that pin. ``os.walk`` could not be made safe here: its own descent
    # re-check is ``os.path.islink``, which a Windows directory junction answers False
    # to, so a junction planted after the screen below was still descended and an entry
    # OUT THERE whose relative name matched the predicate was unlinked -- a delete
    # outside the extracted archive entirely. The extraction directory is only
    # owner-restricted, which does not exclude a same-UID agent process, so the swap
    # needs no cooperation from the archive. A link is removed when the predicate
    # matches it and is never descended either way, because what it points at is not in
    # this archive.
    #
    # The pin starts at the EXTRACTION ROOT, not at ``memory_stores``. Pinning only the
    # leaf leaves its ancestors reached by name at open time, and ``snap`` itself is one
    # of them -- it comes from a by-name ``iterdir()`` in that same owner-only
    # directory, so an agent that swaps ``snap`` for a directory link between the
    # listing and this open redirects the whole strip and the delete lands outside the
    # archive after all. The chain below is what makes the sentence above true:
    # ``snap.parent`` is this process's own freshly created extraction directory and is
    # the anchor, and every component under it is opened THROUGH the pin above it.
    def _empty(pinned: platform_compat.PinnedDirectory) -> None:
        """Remove every entry under *pinned*, through pins the whole way down."""
        for name in sorted(pinned.names()):
            if pinned.is_link(name) or not pinned.is_dir(name):
                pinned.unlink(name)
                continue
            _remove_dir(pinned, name)

    def _remove_dir(pinned: platform_compat.PinnedDirectory, name: str) -> None:
        """Remove the real directory *name* and its whole subtree."""
        sub = pinned.child_if_real_dir(name)
        if sub is None:
            # Replaced since the screen, and being removed either way. A real directory
            # re-raises out of the helper, which is also how the depth refusal past
            # ``PINNED_TREE_MAX_DEPTH`` leaves here: an archive nested past the bound
            # fails the import rather than being imported half-stripped.
            pinned.unlink(name)
            return
        with sub:
            _empty(sub)
        pinned.rmdir(name)

    def _strip(pinned: platform_compat.PinnedDirectory, rel: tuple[str, ...]) -> None:
        for name in sorted(pinned.names()):
            here = (*rel, name)
            matched = is_host_local_store_state((MEMORY_STORES_DIR_NAME, *here))
            if pinned.is_link(name) or not pinned.is_dir(name):
                if matched:
                    pinned.unlink(name)
                continue
            if matched:
                # Removed whole, so its contents are never judged individually.
                _remove_dir(pinned, name)
                continue
            sub = pinned.child_if_real_dir(name)
            if sub is None:
                # Replaced since the screen. Not descended, and NOT removed: the
                # predicate did not match it, so it is not this function's to delete.
                continue
            with sub:
                _strip(sub, here)

    with platform_compat.pinned_directory(snap.parent) as work_pin:
        snap_pin = work_pin.child_if_real_dir(snap.name)
        if snap_pin is None:
            # Not a real directory under the pin: either absent, or replaced with a link
            # since it was listed. Either way there is nothing of THIS archive to strip,
            # and following it is the harm.
            return
        with snap_pin:
            stores_pin = snap_pin.child_if_real_dir(MEMORY_STORES_DIR_NAME)
            if stores_pin is None:
                return
            with stores_pin:
                _strip(stores_pin, ())
