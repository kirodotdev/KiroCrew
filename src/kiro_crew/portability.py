"""Portable zip export/import for KiroCrew state (dashboard endpoint).

Creates a zip archive of all KiroCrew settings and memory for download
via the dashboard, and restores from uploaded zip archives. Designed to
work over HTTP for remote users (e.g. Linux Cloud Desktop → macOS browser).

Credentials (.env, session secrets) are always excluded from exports.
"""

from __future__ import annotations

import contextlib
import importlib as _importlib
import io
import json
import logging
import math
import os
import re
import shutil
import stat
import sys as _sys
import tempfile
import time
import zipfile
import zlib
from collections import deque
from collections.abc import Callable, Iterable, Iterator

# datetime / timezone are the module-level clock binding the export builders read
# (through ``portability_stream`` as ``_pf.datetime`` / ``_pf.timezone``) and the
# one a test freezes with ``ManualClock.install(..., datetime=True)``. The sibling
# module builds the archive timestamps, so this single patch point governs them even
# though the names are referenced only from there.
from datetime import datetime, timezone  # noqa: F401 - module-level clock binding; see above
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import crew_teams, pinned_fs, platform_compat, ui_prefs
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.agent_discovery import clear_list_agents_cache, parsed_agent_specs
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES
from kiro_crew.atomic_write import atomic_write
from kiro_crew.chat_attachments import ATTACHMENTS_DIR_SUFFIX
from kiro_crew.config.loader import (
    ConfigReadError,
    ConfigWriteRefused,
    update_config_locked,
)
from kiro_crew.config.paths import config_dir, kiro_agents_dir
from kiro_crew.history import (
    ARCHIVE_DIR_NAME,
    ARCHIVE_SEGMENT_DELIMITER,
    INCOGNITO_MEMORY_MODES,
    SESSIONS_DIR_NAME,
    THREADS_DIR_NAME,
    THREADS_SIDECAR_SUFFIX,
    TRANSCRIPT_HEADER_MAX_BYTES,
    ConversationLog,
    _safe_key,
    memory_mode_from_header_line,
)
from kiro_crew.mcp_cron import _log_cron_denial, _vet_shell_command
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME, is_host_local_store_state
from kiro_crew.notifications.settings import ChannelSettings, parse_imported_settings
from kiro_crew.platform.governance import sanitize_agent_config_governance
from kiro_crew.portability_archive import (
    _MAX_IMPORT_MEMBERS,
    _MAX_IMPORT_UNCOMPRESSED,
    _MAX_SETTINGS_DOCUMENT_BYTES,
    EXCLUDE_DIRS,
    EXPORT_EXCLUDE,
    MEMORY_EXPORT_MANIFEST_VERSION,
    MEMORY_ONLY_FILES,
    MEMORY_ONLY_TREES,
    _chat_stem,
    _entry_is_link,
    _is_link_entry,
    _is_memory_member,
    _is_rotation_header,
    _mc_dir,
    _read_first_line_fd,
    _rotation_segment_for_stem,
    _transcript_header_verdict,
    _unsafe_member_name,
    logger,
    manifest_is_memory_only,
)
from kiro_crew.portability_templates import (
    AGENT_TEMPLATE_SECRET_PLACEHOLDER,
    AGENT_TEMPLATES_DIR,
    MAX_AGENT_TEMPLATE_BYTES,
    MAX_BUNDLED_AGENT_TEMPLATES,
    MAX_TEMPLATE_NAME_CHARS,
    MAX_TEMPLATE_WARNINGS,
    _install_agent_templates,
    bundled_agent_templates,
    crew_template_refs,
    is_agent_template_stem,
    missing_crew_templates,
    placeholder_mcp_values,
    preview_agent_templates,
    unbundled_agent_templates,
)
from kiro_crew.portability_vetting import (
    _read_manifest_bytes,
    _sanitize_imported_crons,
    _strip_host_local_store_state,
    _top_manifest,
    validate_import_zip,
)
from kiro_crew.security import is_sensitive_path
from kiro_crew.snapshot import (
    _DB_SIDECAR_GLOBS,
    COMPONENTS,
    EXPORT_MANIFEST_VERSION,
    NotificationCopyUnsupported,
    _copy_notifications,
    _copy_tree_no_overwrite,
    _core_file_not_installed,
    _do_replace,
    _install_core_file_if_absent,
    _merge_crons,
    _merge_memory,
    _merge_named_stores,
    _merge_notifications,
    _refuse_corrupt_source_databases,
    _staging_is_pinned,
)
from kiro_crew.user_json import MAX_DOCUMENT_NESTING, exceeds_nesting, strip_utf8_bom
from kiro_crew.zip_vet import ZipInventoryRejected, vet_zip_inventory


class _SettingsRefused(Exception):
    """An archive settings document is not the shape its reader needs; the message says why."""


def _read_archive_text(path: Path) -> str:
    """The archive's *path* as text, read no further than the settings-document cap."""
    with open(path, "rb") as fh:
        raw = fh.read(_MAX_SETTINGS_DOCUMENT_BYTES + 1)
    if len(raw) > _MAX_SETTINGS_DOCUMENT_BYTES:
        raise _SettingsRefused(
            f"the archive's {path.name} is larger than {_MAX_SETTINGS_DOCUMENT_BYTES} bytes"
        )
    return raw.decode("utf-8")


def _read_archive_object(path: Path) -> dict:
    """Parse the archive's *path* as a JSON object, or raise :class:`_SettingsRefused`.

    Only the top-level shape is checked, as the snapshot restore checks it
    (`_refuse_unless_json_object`): every consumer of these files reads an object and
    treats anything else as empty, which is exactly the silent reset an import must not
    install. The archive's ``meta`` block is dropped -- it records the SOURCE install's
    writer and load state, and the destination's own writer stamps a fresh one.
    """
    try:
        doc = json.loads(_read_archive_text(path))
    except (OSError, ValueError, RecursionError) as exc:
        raise _SettingsRefused(f"the archive's {path.name} is not readable JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise _SettingsRefused(
            f"the archive's {path.name} is a JSON {type(doc).__name__}, not an object"
        )
    # Parsing succeeds well past the depth the config readers can walk: a document a
    # few hundred levels deep installs, then every later load raises RecursionError.
    if exceeds_nesting(doc):
        raise _SettingsRefused(
            f"the archive's {path.name} nests deeper than {MAX_DOCUMENT_NESTING} levels"
        )
    doc.pop("meta", None)
    return doc


def _skip_setting(summary: dict, label: str, why: str) -> None:
    summary["items"].append(f"{label} (skipped: {why})")
    summary.setdefault("refused_merges", []).append(label)


def _vet_archive_settings(snap: Path, summary: dict) -> dict[str, object]:
    """Validate every settings document the archive carries, BEFORE either branch applies one.

    Returns the parsed form of each document that passed, keyed by file name. A document
    that failed is reported as skipped and REMOVED from the extracted archive, so neither
    branch can install it: merge never reads it, and replace (`_do_replace`) leaves the
    live copy in place for a file the bundle does not carry.

    The two documents whose readers filter on the way in are also rewritten in the
    extracted archive to their filtered form, so a replace installs what a merge into an
    install without the file would: ``ui-prefs.json`` without its credential-shaped keys
    and unstorable values, ``notification_settings.json`` without a mute on a protected
    channel.
    """
    vetted: dict[str, object] = {}
    for name, label in (
        ("config.json", "config"),
        ("config.local.json", "config.local"),
        ("ui-prefs.json", "ui-prefs"),
        ("notification_settings.json", "notification-settings"),
    ):
        src = snap / name
        if not src.is_file():
            continue
        try:
            if name == "ui-prefs.json":
                try:
                    patch, dropped = ui_prefs.parse_imported_ui_prefs(src)
                except ui_prefs.UiPrefsError as exc:
                    raise _SettingsRefused(f"the archive's {name} is {exc}") from None
                except OSError as exc:
                    raise _SettingsRefused(f"the archive's {name} is unreadable ({exc})") from None
                # Staged as the store's own bytes, so what a Replace installs is the
                # exact document the parse just held to the store's size bound.
                atomic_write(
                    src, ui_prefs.render_document(patch).encode("utf-8"), restrict_to_owner=True
                )
                vetted[name] = (patch, dropped)
            elif name == "notification_settings.json":
                try:
                    text = _read_archive_text(src)
                    channels, dropped = parse_imported_settings(text)
                except (OSError, ValueError) as exc:
                    raise _SettingsRefused(f"the archive's {name} is unusable ({exc})") from None
                # An atomic replace, as the ui-prefs branch above: a by-name
                # write_text would follow a link or hardlink alias planted at the
                # staged name since the read. No mode, matching the live writer.
                atomic_write(src, json.dumps({"channel_settings": channels}, indent=2))
                vetted[name] = (channels, dropped)
            else:
                vetted[name] = _read_archive_object(src)
            # Replace installs the STAGED file's mode (`_backup_and_copy` copies it
            # from the source fstat), and the extractor creates every staged file at
            # the umask default -- typically group/world-readable. The config writer
            # creates at 0o600, so a replace must not install those documents wider
            # than their own writer ever would (the ui-prefs rewrite above already
            # wrote 0o600). notification_settings.json is left alone: its live
            # writer (`atomic_write` with no mode) uses the umask too.
            if name in ("config.json", "config.local.json"):
                # An extracted copy inside the import's private (0o700) temp dir.
                platform_compat.restrict_to_owner(src)  # lockdown-ok: private staged copy
        except _SettingsRefused as exc:
            _skip_setting(summary, label, str(exc))
            src.unlink()
    return vetted


#: What a Merge says about a settings document it left alone. Replace is the path
#: that restores one over this install's own.
_KEPT_NOTE = "kept this install's; import with Replace to restore the archive's"


def _merge_settings(
    mc: Path,
    vetted: dict[str, object],
    summary: dict,
    channel_settings: ChannelSettings | None,
) -> None:
    """Apply the archive's settings documents the way Merge applies everything: never overwrite.

    Per document, not per setting. A document this install LACKS is installed from the
    vetted archive copy and reported ``"<label> (restored)"``; a document this install
    HAS is left untouched, byte for byte, and reported as kept. Replace is the path that
    restores the archive's settings over this install's (backing the old ones up first).

    * ``config.json`` -- installed through `update_config_locked`, which re-checks the
      absence under the config lock, writes owner-only, stamps this install's ``meta``
      and wakes the live watcher.
    * ``config.local.json`` -- NEVER installed by a Merge, even where this install has
      none: the overlay outranks ``config.json`` at load, so an installed copy would set
      every key it names over this install's own config -- the rule ``snapshot._do_merge``
      follows for the same file.
    * ``ui-prefs.json`` -- through the store's validated writer
      (`ui_prefs.install_imported_ui_prefs`), which re-checks the absence under its lock.
    * ``notification_settings.json`` -- through the running gateway's store when there
      is one (`ChannelSettings.install_imported`), so a restored mute applies at once and
      the in-memory copy does not write the old settings back over it.

    Every document the archive carried that this Merge did not apply is named in
    ``summary["settings_kept"]``. The entries are the four file names above, never
    archive-authored text, so the list is bounded by construction (at most four).
    """
    kept: list[str] = []

    def _keep(name: str, label: str, note: str = _KEPT_NOTE) -> None:
        kept.append(name)
        summary["items"].append(f"{label} ({note})")

    incoming = vetted.get("config.json")
    if isinstance(incoming, dict):
        target = mc / "config.json"
        installed: list[bool] = []

        def _install_if_absent(_current: dict) -> dict | None:
            # Inside the config lock: a writer that created the file first wins.
            if os.path.lexists(target):
                return None
            installed.append(True)
            return dict(incoming)

        if os.path.lexists(target):
            _keep("config.json", "config")
        else:
            try:
                update_config_locked(target, mutate=_install_if_absent)
            except (ConfigReadError, ConfigWriteRefused, OSError) as exc:
                _skip_setting(summary, "config", str(exc))
            else:
                if installed:
                    summary["items"].append("config (restored)")
                else:
                    _keep("config.json", "config")

    if "config.local.json" in vetted:
        if os.path.lexists(mc / "config.local.json"):
            _keep("config.local.json", "config.local")
        else:
            _keep(
                "config.local.json",
                "config.local",
                "not installed: a Merge never installs the overlay, which would outrank "
                "this install's config.json; import with Replace to restore the archive's",
            )

    prefs = vetted.get("ui-prefs.json")
    if isinstance(prefs, tuple):
        patch, dropped = prefs
        note = f"; {dropped} unstorable entr{'y' if dropped == 1 else 'ies'} dropped"
        try:
            installed_prefs = bool(patch) and ui_prefs.install_imported_ui_prefs(patch)
        except (ui_prefs.UiPrefsError, OSError) as exc:
            _skip_setting(summary, "ui-prefs", str(exc))
        else:
            if installed_prefs:
                summary["items"].append(f"ui-prefs (restored{note if dropped else ''})")
                summary["ui_prefs_restored"] = True
            elif not patch:
                summary["items"].append(f"ui-prefs (nothing to restore{note if dropped else ''})")
            else:
                _keep("ui-prefs.json", "ui-prefs")

    notes = vetted.get("notification_settings.json")
    if isinstance(notes, tuple):
        channels, dropped = notes
        note = f"; {dropped} invalid value{'' if dropped == 1 else 's'} dropped"
        try:
            live = channel_settings if channel_settings is not None else ChannelSettings()
            installed_notes = live.install_imported(channels)
        except OSError as exc:
            _skip_setting(summary, "notification-settings", str(exc))
        else:
            if installed_notes:
                summary["items"].append(
                    f"notification-settings (restored{note if dropped else ''})"
                )
            else:
                _keep("notification_settings.json", "notification-settings")

    if kept:
        summary["settings_kept"] = kept


def _listing(directory: Path) -> dict[str, os.DirEntry]:
    """The entries of *directory* by name; empty when it cannot be listed."""
    try:
        with os.scandir(directory) as scan:
            return {entry.name: entry for entry in scan}
    except OSError:
        return {}


def _real_dir(entry: os.DirEntry | None) -> bool:
    """A directory that is no link or reparse point, classified from its listing."""
    return entry is not None and not _entry_is_link(entry) and entry.is_dir(follow_symlinks=False)


def _real_file(entry: os.DirEntry | None) -> bool:
    """A regular file that is no link or reparse point, classified from its listing."""
    return entry is not None and not _entry_is_link(entry) and entry.is_file(follow_symlinks=False)


@contextlib.contextmanager
def _open_import_session_file(root: Path, rel: PurePath) -> Iterator[int | None]:
    """Open an extracted file with every component below the extraction root pinned.

    The root is the private extraction directory, not the archive-supplied top-level
    name. Descendants must never be resolved before opening: doing so would bless an
    ancestor replaced by a symlink. The caller owns the yielded descriptor until exit.
    """
    with contextlib.ExitStack() as held:
        fd: int | None = None
        try:
            parent = held.enter_context(
                pinned_fs.open_pinned_descendant_dir(
                    root, rel.parts[:-1], what="imported chat source"
                )
            )
            if parent is None:
                fd = platform_compat.open_file_no_reparse(root / rel, nonblocking=True)
            else:
                fd = os.open(
                    rel.name,
                    os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=parent,
                )
            held.callback(os.close, fd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                fd = None
        except (OSError, pinned_fs.PinnedPathRefusal):
            fd = None
        yield fd


def _copy_import_session_fd(
    mc: Path,
    rel: PurePath,
    fd: int,
    *,
    held: contextlib.ExitStack,
    parents: dict[PurePath, int | None],
    created: dict[PurePath, tuple[int, int]],
) -> bool:
    """Keep each destination pin and created inode until this chat commits or rolls back."""
    if rel.parent not in parents:
        parents[rel.parent] = held.enter_context(
            pinned_fs.open_pinned_descendant_dir(
                mc, rel.parts[:-1], what="imported chat destination", create=True
            )
        )
    parent = parents[rel.parent]

    def record_opened(st: os.stat_result) -> None:
        # The descriptor's identity is available even when the first write fails.
        created[rel] = (st.st_dev, st.st_ino)

    os.lseek(fd, 0, os.SEEK_SET)
    # The primitive takes ownership; the reader's context still owns fd.
    return pinned_fs.copy_file_pinned(
        str(mc / rel),
        str(mc / rel),
        src_fd=os.dup(fd),
        dst_dir_fd=parent,
        dst_name=rel.name if parent is not None else None,
        skip_existing=True,
        on_opened=record_opened,
    )


def _rollback_import_session(
    mc: Path,
    parents: dict[PurePath, int | None],
    created: dict[PurePath, tuple[int, int]],
) -> list[str]:
    """Remove only this call's files, through the pins held for their creation."""
    left = []
    for rel, identity in reversed(created.items()):
        parent = parents[rel.parent]
        try:
            if parent is None:
                removed = pinned_fs.unlink_verified_by_name(mc / rel.parent, rel.name, identity)
            else:
                removed = pinned_fs.unlink_verified(parent, rel.name, identity)
        except OSError:
            removed = False
        if not removed:
            left.append(rel.as_posix())
    return left


def _merge_sessions(snap: Path, mc: Path, summary: dict, *, allow_unpinned: bool) -> None:
    """Add the archive's dashboard chats this install does not have yet.

    Never overwrites: a transcript whose file name already exists here is left as
    it is, along with its threads and images, so a re-import is a no-op and two
    machines' copies of one chat are never spliced. Each candidate is re-judged by
    `_transcript_header_verdict` -- the archive is not trusted to have come from an
    export that applied it -- and by `_chat_stem`, so only dashboard chats, in the
    spelling this platform's history layer writes, are installed.

    Listings select candidates only. Every accepted file is opened with the whole
    extraction-relative chain pinned, then copied from that descriptor into the
    pinned destination with exclusive creation. Transcript and rotation headers
    are judged through the descriptor that supplies the installed bytes. Nothing
    is moved out of the extraction tree. Platforms without directory descriptors
    use the import driver's declared unpinned policy and still copy, never move.

    Records ``sessions_added``, ``sessions_skipped_existing``, ``sessions_withheld``
    and ``sessions_failed`` in *summary*. Copy failures roll back only this call's
    files under the stem lock and use the existing items/refused_merges reporting.
    An archive without a ``sessions/`` tree records nothing.
    """
    if not _real_dir(_listing(snap).get(SESSIONS_DIR_NAME)):
        return
    src = snap / SESSIONS_DIR_NAME
    entries = _listing(src)
    threads_dir = entries.get(THREADS_DIR_NAME)
    threads = _listing(src / THREADS_DIR_NAME) if _real_dir(threads_dir) else {}
    archive_dir = entries.get(ARCHIVE_DIR_NAME)
    segments = _listing(src / ARCHIVE_DIR_NAME) if _real_dir(archive_dir) else {}
    _staging_is_pinned(allow_unpinned=allow_unpinned, what="chat import")
    dest = mc / SESSIONS_DIR_NAME
    history = ConversationLog(base_dir=dest)
    added = existing = withheld = failed = 0
    for name in sorted(entries):
        entry = entries[name]
        stem = _chat_stem(name)
        if stem is None or not _real_file(entry):
            continue
        rel = PurePath(SESSIONS_DIR_NAME, name)
        with _open_import_session_file(snap.parent, PurePath(snap.name) / rel) as fd:
            if fd is None or _transcript_header_verdict(_read_first_line_fd(fd)) is not None:
                withheld += 1
                continue
            # Exclusive file creation alone cannot keep two imports' companions
            # together. Hold the transcript writers' lock through the whole install.
            with history.locked_stems([stem]):
                if os.path.lexists(dest / name):
                    existing += 1
                    continue
                companions: list[PurePath] = []
                sidecar = threads.get(f"{stem}{THREADS_SIDECAR_SUFFIX}")
                if sidecar is not None and _real_file(sidecar):
                    companions.append(PurePath(THREADS_DIR_NAME, sidecar.name))
                images = entries.get(f"{stem}{ATTACHMENTS_DIR_SUFFIX}")
                if images is not None and _real_dir(images):
                    # Attachments are flat: only regular files directly inside.
                    for image in _listing(Path(images.path)).values():
                        if _real_file(image):
                            companions.append(PurePath(images.name, image.name))
                companions.extend(
                    PurePath(ARCHIVE_DIR_NAME, segment.name)
                    for segment in segments.values()
                    if _rotation_segment_for_stem(segment.name, stem) and _real_file(segment)
                )
                with contextlib.ExitStack() as held:
                    parents: dict[PurePath, int | None] = {}
                    created: dict[PurePath, tuple[int, int]] = {}

                    def install(member: PurePath, source_fd: int) -> None:
                        if not _copy_import_session_fd(
                            mc, member, source_fd, held=held, parents=parents, created=created
                        ):
                            raise OSError(f"chat file could not be installed: {member.as_posix()}")

                    try:
                        for companion in companions:
                            member = PurePath(SESSIONS_DIR_NAME) / companion
                            with _open_import_session_file(
                                snap.parent, PurePath(snap.name) / member
                            ) as companion_fd:
                                if companion_fd is None:
                                    continue
                                if companion.parts[0] == ARCHIVE_DIR_NAME:
                                    if not _is_rotation_header(_read_first_line_fd(companion_fd)):
                                        continue
                                install(member, companion_fd)
                        # The transcript is the existing-chat marker; publish it last.
                        install(rel, fd)
                    except (OSError, pinned_fs.PinnedPathRefusal) as exc:
                        left = _rollback_import_session(mc, parents, created)
                        failed += 1
                        summary["items"].append(f"sessions/{name} (failed: {exc})")
                        if left:
                            summary["items"].append(
                                "sessions rollback incomplete (not removed: "
                                + ", ".join(left)
                                + ")"
                            )
                        continue
                added += 1
    summary["sessions_added"] = added
    summary["sessions_skipped_existing"] = existing
    summary["sessions_withheld"] = withheld
    summary["sessions_failed"] = failed
    if failed:
        summary.setdefault("refused_merges", []).append("sessions")
    summary["items"].append(
        f"sessions (merged: {added} added, {existing} already here, {withheld} withheld, "
        f"{failed} failed)"
    )


def _read_session_mtimes(zf: zipfile.ZipFile) -> dict[str, float]:
    """Read optional absolute session times; unusable records leave extraction time.

    This is bounded by both the settings-document byte cap and archive member cap.
    ZIP wall-clock fields carry no zone, so they are never a fallback for activity.
    """
    manifests = [
        info
        for info in zf.infolist()
        if len(PurePosixPath(info.filename).parts) == 2
        and PurePosixPath(info.filename).name == "MANIFEST.json"
    ]
    if len(manifests) != 1 or manifests[0].file_size > _MAX_SETTINGS_DOCUMENT_BYTES:
        return {}
    try:
        with zf.open(manifests[0]) as src:
            raw = src.read(_MAX_SETTINGS_DOCUMENT_BYTES + 1)
        if len(raw) > _MAX_SETTINGS_DOCUMENT_BYTES:
            return {}
        manifest = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    records = manifest.get("session_mtimes")
    if not isinstance(records, dict) or len(records) > _MAX_IMPORT_MEMBERS:
        return {}
    mtimes: dict[str, float] = {}
    for name, value in records.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            epoch = float(value)
        except OverflowError:
            continue
        if math.isfinite(epoch):
            mtimes[name] = epoch
    return mtimes


def _read_member_json(zf: zipfile.ZipFile, name: str | zipfile.ZipInfo) -> object:
    """One archive member parsed as JSON, or ``None`` when it does not parse.

    An oversized member raises ``ValueError`` instead of reading as ``None``: the
    caller asks whether ANY manifest declares memory only, and an unreadable one
    must not quietly answer "no" and widen the import.
    """
    data = _read_manifest_bytes(zf, name)
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return None


def apply_import_zip(
    zip_path: Path,
    mode: str = "merge",
    *,
    channel_settings: ChannelSettings | None = None,
    memory_only: bool = False,
    agent_templates: tuple[str, ...] = (),
) -> dict:
    """Extract and apply an import zip.

    Args:
        zip_path: Path to validated zip file.
        mode: "merge" (default, non-destructive) or "replace" (overwrites). Merge
            never overwrites memory, crons, workspace files or skills, and installs
            an archive settings document only where this install has none -- never
            the ``config.local.json`` overlay -- naming each it kept in
            ``settings_kept`` (see `_merge_settings`). Replace restores all four.
        channel_settings: the running gateway's notification-settings store, so a
            restored mute applies at once and is not written back over by the
            in-memory copy. ``None`` (no gateway in this process) loads one from disk.
        memory_only: apply only the memory component (`_is_memory_member`); every
            other member stays in the zip. Also implied by an archive whose manifest
            declares memory only. Merge only: a Replace raises ``ValueError``,
            because a whole-install replace from a memory bundle would reset
            everything the bundle does not carry.
        agent_templates: the bundled agent templates (file stems under ``agents/``) to
            install. Empty, the default, installs none. A picked template is written
            only where no template of that name exists, in either mode; every one not
            written is named in ``agent_templates_skipped``. A memory-only import
            installs none.

    Returns summary dict of what was imported. ``ui_prefs_restored`` is True when the
    browser-preference backup was installed, which tells the dashboard to re-read it.
    """
    try:
        vet_zip_inventory(zip_path, max_members=_MAX_IMPORT_MEMBERS)
    except ZipInventoryRejected as exc:
        if exc.reason == "too_many_members":
            raise ValueError(f"Import archive has too many entries ({exc})") from exc
        if exc.reason in {"cdir_too_large", "zip64_saturated"}:
            raise ValueError(
                f"Import archive inventory exceeds cap ({exc}; possible zip bomb)"
            ) from exc
        raise zipfile.BadZipFile("Invalid zip file") from exc

    mc = _mc_dir()
    # Asked once, at the top, before anything is extracted or written. Both branches
    # below mutate the data home, and the merge branch writes core files with
    # shutil.copy2 BEFORE it reaches the first tree call -- so gating inside the tree
    # helpers let a merge on a platform that cannot pin half-apply the core files and
    # then raise, against this function's own "fails loudly, nothing was written"
    # contract. Raised in review. This is the third site of the same defect (after
    # _build_snapshot and _do_merge), which is why the gate now lives at the entry of
    # every operation that mutates rather than next to the individual writes.
    #
    # There is no flag to pass here: an import is a UI action, not a command line. I first
    # concluded from that that the gate should refuse, and it was the wrong conclusion drawn
    # from a correct observation -- CI proved it by failing four PRE-EXISTING portability
    # tests on Windows. Refuse-by-default only means "ask the user" where a consent surface
    # exists. Where none does, it means removing the feature on that platform, which is not
    # a security decision anyone made.
    #
    # So this path PERMITS a by-name traversal and records that it happened, while snapshot
    # and restore keep refusing -- because they have `--allow-unpinned-staging` and can
    # actually ask. The per-entry screens still apply either way: the copy opens with
    # O_NOFOLLOW and the walk rejects links and reparse points, so what is given up here is
    # ancestor-swap resistance, not link resistance.
    staging_pinned = _staging_is_pinned(allow_unpinned=True, what=f"{mode} import")

    # What this field may honestly say depends on the MODE, not only the platform.
    # Reporting "pinned" for a merge whose core files and skills are still copied by
    # name with `shutil` is true of the platform, false of the operation, and this
    # field exists to tell a reader what actually happened.
    #
    # replace delegates the whole apply to `_do_replace`, which is pinned throughout. merge
    # routes only its tree copy through the primitive; its core files (including the
    # databases, deliberately out of scope) and its skills copy are by name. So merge
    # on a pinnable platform is MIXED, and saying so is the point.
    if not staging_pinned:
        staging_mode = "unpinned"
    elif mode == "replace":
        staging_mode = "pinned"
    else:
        staging_mode = "mixed"
    if not staging_pinned:
        logger.warning(
            "%s import staged by name: this platform cannot open a directory relative to a "
            "descriptor, so an ancestor swapped mid-import could redirect the copy. The "
            "summary records staging=unpinned.",
            mode,
        )
    summary: dict = {
        "mode": mode,
        "items": [],
        "staging": staging_mode,
    }

    with tempfile.TemporaryDirectory() as work_str:
        work = Path(work_str)
        platform_compat.restrict_dir_to_owner(work)

        with zipfile.ZipFile(str(zip_path), "r") as zf:
            infos = zf.infolist()
            # Zip-bomb guard (defense-in-depth; validate_import_zip also checks).
            if len(infos) > _MAX_IMPORT_MEMBERS:
                raise ValueError(
                    f"Import archive has too many entries ({len(infos)} > {_MAX_IMPORT_MEMBERS})"
                )
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > _MAX_IMPORT_UNCOMPRESSED:
                raise ValueError(
                    f"Import archive uncompressed size {total_uncompressed} exceeds cap "
                    f"{_MAX_IMPORT_UNCOMPRESSED} (possible zip bomb)"
                )
            session_mtimes = _read_session_mtimes(zf)
            if not memory_only:
                # The same one manifest the validator read decides the scope; a file
                # elsewhere that happens to be named MANIFEST.json is content. With no
                # manifest at all nothing declares memory only (the validator is what
                # requires one); two at the top are still refused.
                top = _top_manifest(infos, required=False)
                memory_only = top is not None and manifest_is_memory_only(
                    _read_member_json(zf, top)
                )
            if memory_only and mode != "merge":
                raise ValueError("A memory-only import is merge only")
            for info in infos:
                parts = PurePosixPath(info.filename).parts
                if _unsafe_member_name(info.filename):
                    continue
                if _is_link_entry(info):
                    continue
                if memory_only and not _is_memory_member(tuple(parts[1:])):
                    continue
                if len(parts) >= 2 and parts[1] == AGENT_TEMPLATES_DIR:
                    # Bundled templates are their own component, installed below
                    # straight from the archive and only when picked.
                    continue
                extracted = zf.extract(info, work)
                # The filters above read the NAME; this checks where the extractor
                # actually put it. A member that lost its top directory on the way
                # out would be read as a different component than it was filtered as.
                if Path(extracted).relative_to(work).parts[:1] != parts[:1]:
                    raise ValueError(
                        f"Rejected member that left its archive prefix: {info.filename}"
                    )
                if len(parts) >= 3 and parts[1] == SESSIONS_DIR_NAME and not info.is_dir():
                    # Only accepted chats are copied out of this private staging tree;
                    # no-overwrite copying preserves these epochs on installed files.
                    ts = session_mtimes.get(info.filename)
                    if ts is not None:
                        with contextlib.suppress(OSError, OverflowError, ValueError):
                            os.utime(extracted, (ts, ts))

        snap_dirs = [d for d in work.iterdir() if d.is_dir()]
        if len(snap_dirs) != 1:
            raise ValueError(f"Expected 1 top-level directory in zip, found {len(snap_dirs)}")
        snap = snap_dirs[0]
        if memory_only:
            summary["components"] = ["memory"]

        # Re-vet imported cron commands before ANY path below consumes
        # crons.json (merge, copy, or replace). An import archive is
        # attacker-influenced — the threat is a "settings backup" a user is
        # talked into importing — and a cron ``command`` is a free-form shell
        # string the scheduler later runs via ``sh -c``, entirely outside the
        # ACP permission/hook flow. ``cron_add`` guards exactly that out-of-band
        # execution with ``_vet_shell_command`` at storage time; the import path
        # wrote crons.json directly and so skipped it, turning a crafted archive
        # into arbitrary command execution with no CSRF, prompt injection, or
        # auth bypass required (CWE-502, CWE-862). Apply the identical guard here
        # and drop any job that fails, so a mostly-benign backup still restores
        # its safe jobs instead of the whole import aborting.
        dropped_crons, paused_crons = _sanitize_imported_crons(snap / "crons.json")
        if dropped_crons:
            summary["rejected_crons"] = dropped_crons
        # Reported separately: these are restored in full and only need switching
        # on, so calling them "rejected" would tell the user their jobs are gone.
        if paused_crons:
            summary["paused_crons"] = paused_crons

        # Before EITHER branch, for the same reason the cron re-vet is: both branches copy
        # the tree below. No export writes these entries, so an archive carrying one was
        # built by hand -- and the member signing key is the one that matters, because
        # installing it would let the archive's author sign as this host's members.
        _strip_host_local_store_state(snap)

        # The memory component's incoming databases are validated before EITHER branch
        # moves anything, with the restore's own validator: replace installs everything
        # the archive carries, merge only what the destination lacks, and a named store
        # the destination lacks is exactly the merge case -- a whole-store copy
        # would otherwise install a torn `memory_stores/<name>/memory.db` verbatim, and
        # the member fails at its next open with the archive long gone. Refused here,
        # nothing has been written; the exception reaches the handler as a refusal.
        # `crew-teams` rides the same check: replace copies the tree whole through
        # `_do_replace`, merge copies the document where the destination has none, and
        # both would otherwise install a document the team store's reader refuses.
        _refuse_corrupt_source_databases(
            snap, ["memory", "crew-teams"], mc_for_merge=None if mode == "replace" else mc
        )

        # Before either branch too: a settings document neither branch may install is
        # removed from the extraction here, and reported.
        vetted_settings = _vet_archive_settings(snap, summary)

        if mode == "replace":
            # Strip sensitive files and skills/auto/ from snapshot before replace
            for excluded_name in EXPORT_EXCLUDE:
                excluded_file = snap / excluded_name
                if excluded_file.exists():
                    excluded_file.unlink()
            for fpath in snap.rglob("*"):
                if fpath.is_file() and is_sensitive_path(str(fpath)):
                    fpath.unlink()
            auto_dir = snap / "skills" / "auto"
            # ``is_link_or_junction`` FIRST, not a bare ``is_dir()``: a Windows
            # directory JUNCTION answers ``is_dir()`` True and ``is_symlink()``
            # False, and it is the only directory link an unprivileged Windows
            # writer can plant in the extraction tree. ``shutil.rmtree`` follows
            # a junction into its target, so an ``is_dir()``-only guard let a
            # junction planted at this name aim the delete OUTSIDE the extracted
            # archive. ``unlink_link_or_junction`` removes the LINK, never what it
            # points at; only a real directory reaches ``rmtree``.
            if platform_compat.is_link_or_junction(auto_dir):
                platform_compat.unlink_link_or_junction(auto_dir)
            elif auto_dir.is_dir():
                shutil.rmtree(str(auto_dir))
            # A platform that cannot pin a directory by descriptor refuses this
            # staging pass, and the refusal is allowed to propagate. An earlier
            # revision caught it and returned the summary, which was worse than the
            # crash it avoided: the caller reads a returned summary as success, so the
            # dashboard rendered "Import complete" over a data home nothing had been
            # written to. Raised in review. Propagating reaches the existing error
            # path, which is the one that tells the user the import did not happen.
            # Each settings store whose file this swap replaces is held at its writer
            # lock for the whole swap (rollback included): a write from another tab or
            # channel in between would publish its pre-import copy over the restored
            # file. The notification store also re-reads its file before letting go.
            # Taken in one fixed order, ui-prefs then notifications.
            with contextlib.ExitStack() as swap_guard:
                if "ui-prefs.json" in vetted_settings:
                    swap_guard.enter_context(ui_prefs.replacing_file())
                if channel_settings is not None and "notification_settings.json" in vetted_settings:
                    channels, _dropped = cast(
                        "tuple[dict[str, dict[str, Any]], int]",
                        vetted_settings["notification_settings.json"],
                    )
                    swap_guard.enter_context(channel_settings.replacing_file(channels))
                _do_replace(snap, mc, None, allow_unpinned=not staging_pinned)
            summary["items"].append("full replace")
            if "ui-prefs.json" in vetted_settings:
                summary["ui_prefs_restored"] = True
        else:
            # Merge mode
            if (snap / "memory.db").is_file():
                if not (mc / "memory.db").is_file():
                    # Pinned, not copy2: see `_install_core_file_if_absent`.
                    if _install_core_file_if_absent(snap / "memory.db", mc / "memory.db"):
                        summary["items"].append("memory (copied)")
                        if (snap / "memory_index.db").is_file() and not (
                            _install_core_file_if_absent(
                                snap / "memory_index.db", mc / "memory_index.db"
                            )
                        ):
                            # Never overwritten, so a leftover index does not match
                            # the memory.db just installed; see `_core_file_not_installed`.
                            summary["items"].append(
                                _core_file_not_installed(
                                    mc / "memory_index.db", index=True, dashboard=True
                                )
                            )
                            summary.setdefault("refused_merges", []).append("memory_index")
                    else:
                        summary["items"].append("memory (skipped: not a regular file)")
                        summary.setdefault("refused_merges", []).append("memory")
                else:
                    _merge_memory(snap / "memory.db", mc / "memory.db")
                    summary["items"].append("memory (merged)")

            if (snap / "crons.json").is_file():
                if (mc / "crons.json").is_file():
                    if _merge_crons(snap / "crons.json", mc / "crons.json"):
                        summary["items"].append("crons (merged)")
                    else:
                        # A refused merge imported zero jobs. Appending
                        # "crons (merged)" here regardless would render a
                        # success over a restore that brought no job back.
                        # The refusal is named in the
                        # items and flagged machine-readably so the handler can
                        # log the import as partial rather than a flat ok.
                        summary["items"].append("crons (skipped: unreadable or invalid cron store)")
                        summary.setdefault("refused_merges", []).append("crons")
                elif _install_core_file_if_absent(snap / "crons.json", mc / "crons.json"):
                    summary["items"].append("crons (copied)")
                else:
                    summary["items"].append("crons (skipped: not a regular file)")
                    summary.setdefault("refused_merges", []).append("crons")

            # The team list, like hooks.json: installed only where the destination has
            # none -- decided and written under the store's own lock, document only, so
            # a concurrent team write neither loses to the import nor overwrites it.
            # Validated above by the team store's own reader, before anything moved.
            teams_snap = snap / crew_teams.TEAMS_DIR_NAME / crew_teams.TEAMS_FILE_NAME
            if teams_snap.is_file() and crew_teams.install_document(
                teams_snap, mc / crew_teams.TEAMS_DIR_NAME, only_if_absent=True
            ):
                summary["items"].append("crew-teams (copied)")

            if (snap / "hooks.json").is_file():
                if not (mc / "hooks.json").is_file():
                    if _install_core_file_if_absent(snap / "hooks.json", mc / "hooks.json"):
                        summary["items"].append("hooks (copied)")
                    else:
                        summary["items"].append("hooks (skipped: not a regular file)")
                        summary.setdefault("refused_merges", []).append("hooks")
                else:
                    summary["items"].append("hooks (skipped, already exists)")

            _merge_settings(mc, vetted_settings, summary, channel_settings)

            if (snap / "notifications.jsonl").is_file():
                if (mc / "notifications.jsonl").is_file():
                    # A platform that cannot pin raises
                    # NotificationCopyUnsupported from inside the merge, exactly
                    # as the copy branch does below -- record it as skipped and
                    # let the import proceed. A link/FIFO/hardlink refusal on a
                    # capable platform raises OSError and aborts instead, because
                    # that is a bad or hostile source.
                    try:
                        _merge_notifications(
                            snap / "notifications.jsonl", mc / "notifications.jsonl"
                        )
                        summary["items"].append("notifications (merged)")
                    except NotificationCopyUnsupported as exc:
                        # Zero records imported: flag it machine-readably so the
                        # handler logs the import as partial, not a flat ok --
                        # exactly as the crons refusal above does. A flat ok would
                        # tell the API caller the import succeeded over records
                        # left behind, the failure class this whole change removes.
                        summary["items"].append(f"notifications (SKIPPED: {exc})")
                        summary.setdefault("refused_merges", []).append("notifications")
                else:
                    # Not `copy2`: it installed records the live file's own reader
                    # refuses, and that reader loses the whole file to one of them.
                    # Same abort posture as the merge branch above.
                    #
                    # The platform refusal is NOT that abort: it says this platform
                    # can never do this safely, so it skips one item and lets the
                    # import proceed. Recorded in the summary as skipped WITH the
                    # reason -- reporting "copied" for a refusal, or saying nothing,
                    # would be the silent-install bug class this change removes.
                    try:
                        _copy_notifications(
                            snap / "notifications.jsonl", mc / "notifications.jsonl"
                        )
                        summary["items"].append("notifications (copied)")
                    except NotificationCopyUnsupported as exc:
                        summary["items"].append(f"notifications (SKIPPED: {exc})")
                        summary.setdefault("refused_merges", []).append("notifications")

            for dirname in ("workspace", "plan_memory"):
                sd = snap / dirname
                if sd.is_dir():
                    dd = mc / dirname
                    dd.mkdir(parents=True, exist_ok=True)
                    # The permission decided once at entry flows down; otherwise the inner
                    # gate re-asks and refuses, which is the same platform outage by a
                    # longer route.
                    _copy_tree_no_overwrite(sd, dd, allow_unpinned=not staging_pinned)
                    summary["items"].append(f"{dirname} (merged)")

            stores_src = snap / MEMORY_STORES_DIR_NAME
            if stores_src.is_dir():
                stores_dst = mc / MEMORY_STORES_DIR_NAME
                kept = _merge_named_stores(
                    stores_src, stores_dst, allow_unpinned=not staging_pinned
                )
                summary["items"].append(f"{MEMORY_STORES_DIR_NAME} (merged)")
                for store in kept:
                    summary["items"].append(
                        f"{MEMORY_STORES_DIR_NAME}/{store} (kept the existing store; "
                        "the archive's copy was not merged into it)"
                    )

            if (snap / "skills").is_dir():
                (mc / "skills").mkdir(parents=True, exist_ok=True)
                # Skip skills/auto/ — those must go through SkillsLoader APIs
                for item in (snap / "skills").iterdir():
                    if item.name == "auto":
                        continue
                    target = mc / "skills" / item.name
                    if item.is_dir() and not target.exists():
                        shutil.copytree(str(item), str(target))
                    elif item.is_file() and not target.exists():
                        shutil.copy2(str(item), str(target))
                summary["items"].append("skills (merged, auto/ skipped)")

        # Both modes, and add-only in both: Replace restores the components it
        # names, and chats are not one of them, so an archive -- with chats or
        # without -- never deletes or overwrites a chat this install already has.
        _merge_sessions(snap, mc, summary, allow_unpinned=not staging_pinned)

    if agent_templates and not memory_only:
        _install_agent_templates(zip_path, sorted(set(agent_templates)), summary)

    # Warn, never refuse: the rows are already written, and a template can be
    # installed afterwards without importing again.
    try:
        missing, more = missing_crew_templates(mc / "config.json")
    except Exception:  # RecursionError included
        # The import is already written: a spec this cannot read costs the warning,
        # never the import's success.
        logger.warning("Could not check crew agent templates after import", exc_info=True)
        missing, more = [], 0
    if missing:
        summary["missing_agent_templates"] = missing
    if more:
        summary["missing_agent_templates_more"] = more
    return summary


# The export-archive building API lives in a sibling module so this file stays
# under the file-line cap; it is re-exported here so the import path callers use
# (``from kiro_crew.portability import plan_export`` / ``stream_export_zip`` /
# ``create_export_zip`` / ``ExportPlan``) is unchanged. The import is at the bottom
# because portability_stream reads this module's helpers at call time, so every
# name it needs must already be defined when it is imported.
from kiro_crew.portability_stream import (  # noqa: E402,F401 - deferred re-export; see above
    ExportPlan,
    create_export_zip,
    plan_export,
    stream_export_zip,
)

# This module is the facade of the portable export and import. It keeps the import --
# :func:`apply_import_zip` and the settings and session merges it runs -- and every name
# its owners define still resolves as ``kiro_crew.portability.<name>``. A write through it
# (``monkeypatch.setattr``, ``mock.patch``) reaches every owner holding the name
# (``kiro_crew._facade_mirror``). No owner imports this module; an owner imports only the
# owners listed before it.
# The owners log as ``kiro_crew.portability``. New work goes to the owner of its
# responsibility:
#   ``portability_archive`` -- what an archive holds, member names and limits both ways
#   ``portability_templates`` -- agent templates: bundled on export, installed on import
#   ``portability_export`` -- what an export reads: the walk, the SQLite backups, the sessions
#   ``portability_vetting`` -- validate_import_zip and the staged cron and store checks
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.portability_archive",
    "kiro_crew.portability_templates",
    "kiro_crew.portability_export",
    "kiro_crew.portability_vetting",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: the unsplit module's public names, and TYPE_CHECKING.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.portability_archive import (  # noqa: F401
        _DASHBOARD_STEM_PREFIX,
        _DRIVE_PREFIX,
        _FILE_ATTRIBUTE_REPARSE_POINT,
        _MAX_TRANSCRIPT_HEADER_BYTES,
        _SETTINGS_DOCUMENTS,
        _TRANSCRIPT_SUFFIX,
        _is_excluded,
        _keep_top_level_transcript,
        _transcript_stem,
    )
    from kiro_crew.portability_export import (  # noqa: F401
        _MANIFEST_BASE_RESERVE,
        _MANIFEST_PER_MEMBER_RESERVE,
        _SQLITE_COPY_CHUNK_BYTES,
        _ZIP_DATE_MAX,
        _ZIP_DATE_MIN,
        _add_from_fd,
        _add_session_from_fd,
        _backup_sqlite,
        _chat_companions,
        _keep_for_export,
        _keep_store_for_export,
        _manifest_entry_size,
        _open_contained_file,
        _open_verified,
        _plan_sessions,
        _session_candidates,
        _session_manifest_base,
        _SessionOrder,
        _sqlite_logical_size,
        _wal_checkpoint,
        _walk_contained,
        _walk_pinned,
        _write_sessions,
        _zip_date_time,
    )
    from kiro_crew.portability_templates import (  # noqa: F401
        _MANAGED_TEMPLATES,
        _SUBAGENT_REF_KEYS,
        _TEMPLATE_STEM_RE,
        _UNREADABLE_MEMBER,
        _archive_agent_templates,
        _clip,
        _installed_specs,
        _installed_template_names,
        _template_refs_in_spec,
        _vet_bundled_template,
    )
    from kiro_crew.portability_vetting import (  # noqa: F401
        _UNREADABLE_STORE,
        _publish_staged_cron_store,
        _read_staged_cron_store,
        _sanitize_cron_text,
    )
