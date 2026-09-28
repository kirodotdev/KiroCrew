"""Kiro Crew snapshot and restore — portable state management.

The command and API facade. ``snapshot`` and ``restore`` are implemented here, together
with the seam that prepares a redacted copy for an off-host upload. ``restore_main``
sequences extraction and the bundle-shape refusals, and writes their
``state_restore_rejected`` audits. The rules both commands apply live in owner modules and
are re-exported below, so every existing import keeps resolving. The owners read the
helpers, drivers and limits a test replaces through this module at call time
(:func:`kiro_crew.snapshot_components._facade`), so a patch of one of those names here
reaches the owners' call sites too. Every other name resolves in the owner that defines or
imports it, and a patch of it belongs on that owner.

* :mod:`kiro_crew.snapshot_components` -- the component table and the tree-root check
* :mod:`kiro_crew.snapshot_archive` -- staging and the bundle format: the pinned copy, the
  extraction filter, the archive bound and the manifest readers
* :mod:`kiro_crew.snapshot_restore` -- the bundle predicates, the content-soundness refusal,
  the destination guards, and the replace transaction with its rollback
* :mod:`kiro_crew.snapshot_merge` -- merge-mode algorithms
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import socket
import stat as _stat
import sys
import tarfile
import tempfile
from contextlib import ExitStack, closing
from dataclasses import dataclass  # noqa: F401 - facade re-exports
from datetime import datetime, timezone
from enum import Enum  # noqa: F401 - facade re-exports
from pathlib import Path, PurePosixPath, PureWindowsPath  # noqa: F401 - facade re-exports
from typing import TYPE_CHECKING, Any, Callable  # noqa: F401 - facade re-exports

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.jsonl_util import (  # noqa: F401 - facade re-exports
    RECORD_CAP,
    UndecodableRecord,
    UnreadableRecord,
    strict_raw_records,
)
from kiro_crew.member_memory_backup import (  # noqa: F401 - facade re-exports
    StoresInUse,
    hold_stores_for_read,
    hold_stores_for_replace,
)
from kiro_crew.memory_stores import (  # noqa: F401 - facade re-exports
    MEMBER_BACKUPS_DIR_NAME,
    MEMORY_STORES_DIR_NAME,
    is_host_local_store_state,
    memory_store_namespace_lock,
    named_store_product_file,
)
from kiro_crew.snapshot_archive import (  # noqa: F401 - facade re-exports
    _CONTROL_CHARS,
    _DB_SIDECAR_GLOBS,
    _DB_SUFFIXES,
    _FIRST_EXPORT_VERSION_WITH_NAMED_STORES,
    _FIRST_VERSION_WITH_NAMED_STORES,
    _MAX_ARCHIVE_BYTES,
    _MAX_ARCHIVE_MEMBERS,
    DB_COPIED,
    DB_NOT_A_DATABASE,
    DB_UNSAFE_SOURCE,
    EXPORT_MANIFEST_VERSION,
    MANIFEST_VERSION,
    SKIP_DB_UNPINNED_SOURCE,
    DatabaseCopyFailed,
    ManifestUnreadable,
    _ArchiveTooLarge,
    _bundle_carries_named_stores,
    _chain_is_link_free,
    _copy_database_consistently,
    _copytree_safe,
    _data_filter,
    _dir_flags_nofollow,
    _escape_one,
    _manifest_components,
    _print_manifest,
    _refuse_oversized_archive,
    _refuse_unsound_required_capture,
    _rejection_recording_filter,
    _report_skip,
    _restage_databases,
    _safe_name,
    _staging_ignore,
    _staging_is_pinned,
    _terminal_safe,
)
from kiro_crew.snapshot_components import (  # noqa: F401 - facade re-exports
    _CORE_FILE_COMPONENTS,
    _DERIVED_INDEXES,
    _HOST_LOCAL_PATHS,
    _JSON_OBJECT_LISTS,
    _LOCKED_DOCUMENT_TREES,
    _REPLACE_ONLY_COMPONENTS,
    _TREE_DOCUMENT_VALIDATORS,
    _WHOLE_TREE_COMPONENTS,
    COMPONENT_HELP,
    COMPONENT_JSON_OBJECTS,
    COMPONENT_TREES,
    COMPONENTS,
    CORE_FILES,
    CORE_FILES_FLAT,
    NEVER_SNAPSHOT_FILES,
    PRODUCT_TREE_DATABASES,
    SECURITY_SENSITIVE_FILES,
    VALID_COMPONENTS,
    ComponentRefused,
    ComponentSpec,
    Purpose,
    SecretPolicy,
    UnsafeComponentRoot,
    _facade,
    _is_host_local,
    _mc_dir,
    _never_ships,
    _tree_roots_replace_clears,
    _want,
    is_product_tree_database,
    resolve_components,
    safe_tree_root,
)
from kiro_crew.snapshot_merge import (  # noqa: F401 - facade re-exports
    _MERGE_ALLOWED_TABLES,
    _NOTIFICATION_RECORD_CAP,
    _NOTIFICATION_SOURCE_CAP,
    _SAFE_IDENTIFIER_RE,
    _TELEMETRY_SALT_BYTES,
    _TERMINATORS,
    NotificationCopyUnsupported,
    _copy_locked,
    _copy_tree_no_overwrite,
    _install_notifications,
    _merge_crons,
    _merge_memory,
    _merge_named_stores,
    _merge_notifications,
    _notification_key,
    _report_unmerged_databases,
    _serialise_with_notification_writes,
    _usable_cron_shape,
    _validate_identifier,
)
from kiro_crew.snapshot_restore import (  # noqa: F401 - facade re-exports
    NamedStoresInUse,
    RollbackIncomplete,
    SourceComponentUnsound,
    _allocate_rollback_dir,
    _backup_and_copy,
    _backup_tree_or_refuse,
    _clear_store_directories,
    _component_payload_absent,
    _components_absent_from_bundle,
    _do_replace,
    _do_replace_mutations,
    _drop_derived_indexes_absent_from_bundle,
    _install_locked_document,
    _lock_down_restored,
    _refuse_corrupt_source_databases,
    _refuse_unless_json_object,
    _refuse_unless_sound,
    _refuse_unless_valid_tree_document,
    _refuse_unsafe_destination_roots,
    _remove_locked_document,
    _restore_everything_from_rollback,
    _restore_locked_document,
    _save_locked_document_to,
    _trees_absent_from_bundle,
)

if TYPE_CHECKING:
    from kiro_crew import snapshot_redact

from kiro_crew._sqlite_compat import sqlite3

try:
    from kiro_crew.config.loader import DASHBOARD_PORT as _DASHBOARD_PORT
except Exception:  # pragma: no cover - optional during early/standalone import
    _DASHBOARD_PORT = int(os.environ.get("KIROCREW_PORT", 5476))


def _redactor() -> Any:
    """The outbound redaction pass, imported on first use.

    `snapshot` is on the gateway's boot path, so importing the redaction code here would
    put it there too -- and a gateway never redacts anything. Resolved when a command that
    actually prepares an outbound copy asks for it, so `kirocrew gateway` reaches
    readiness without loading it. A ratchet compares the boot module set against the base
    branch's.
    """
    return importlib.import_module("kiro_crew.snapshot_redact")


def _default_snapshot_dir() -> str:
    """Return snapshot directory from config, falling back to <config_dir>/snapshots."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        d = KiroCrewConfig.load().snapshot_dir
        if d:
            return str(Path(d).expanduser())
    except Exception:
        pass
    try:
        from kiro_crew.config.paths import config_dir

        return str(config_dir() / "snapshots")
    except Exception:
        return str(Path.home() / ".kiro" / "crew" / "snapshots")


def _audit(event_type: str, resources: str) -> None:
    """Emit a SEL audit event for snapshot/restore operations."""
    try:
        from kiro_crew.sel import SecurityEvent, sel

        sel().log(
            SecurityEvent(
                event_id=os.urandom(8).hex(),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=event_type,
                caller_identity=os.environ.get("USER", "unknown"),
                agent="kirocrew",
                source="cli",
                operation=event_type,
                outcome="completed",
                resources=resources,
            )
        )
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("SEL audit event '%s' failed: %s", event_type, e)


def _fsize(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _list_components() -> None:
    print("Available components:")
    for k, v in COMPONENT_HELP.items():
        print(f"  {k:16s} {v}")
    print("\nCombine with commas: --components memory,crons,skills")


def _report_unredacted_upload() -> None:
    """Say plainly what an operator gets by turning redaction off."""
    print(
        "⚠️  Redaction is DISABLED by the switch in your backup directory, so this upload "
        "carries credential material in plaintext."
    )
    print(
        "   That is what makes the off-host copy restore complete. The bucket was "
        "verified private at setup and every write asserts your account owns it, but "
        "anyone who can read the bucket can read your credentials — treat it as secret."
    )


def _report_unresolved_payload(selected: list[str]) -> None:
    """Name the components in this bundle that carry uncertified credential material.

    A backup is NOT redacted, and that is deliberate: it goes to a destination the
    operator provisioned in their own account, and stripping a credential out of a backup
    produces an archive that cannot restore a working install — the token is part of the
    state being protected. The `SHARE` purpose is where content leaves the operator's
    control, and it refuses every component today precisely because no component has been
    certified safe to hand to someone else.

    What that reasoning does NOT cover is an operator who does not know what is in the
    bundle. A backup with no `--components` stages everything, which includes the config
    file holding a bot token in plaintext. So the bundle's credential-bearing contents are
    named on the way out. The operator keeps the un-redacted backup they need, and learns
    what they are sending without having to read the component table to find out.
    """
    riding = [name for name in selected if COMPONENTS[name].policy is SecretPolicy.UNRESOLVED]
    if not riding:
        return
    print(f"ℹ️  Riding this bundle, uncertified for sharing: {', '.join(sorted(riding))}.")
    print(
        "   `config` carries credentials in plaintext at rest. Whether the copy that "
        "leaves this host still does is reported below; the bundle on local disk always "
        "does, so treat it as secret and narrow it with --components if you do not need "
        "all of it."
    )


class RedactionFailed(RuntimeError):
    """Redaction could not be completed, so there is nothing safe to upload.

    A `RuntimeError` on purpose, and not narrowable back to `Exception`. The off-host
    caller is the AWS Control app's backup route, whose error contract is already
    `AWSError -> aws_failed` and `RuntimeError -> backup_failed`; as a plain `Exception`
    this refusal escaped both and surfaced as an HTTP 500 with no machine-readable code, so
    an operator saw a crash where the product had actually made a correct safety decision.
    Verified before widening that nothing on the snapshot, redaction or app-backup path
    catches `RuntimeError`, so this cannot be swallowed into "send it unredacted" -- which
    would be far worse than the 500 it replaces.
    """


def _redacted_upload_copy(
    outfile: Path, workdir: Path
) -> "tuple[Path, snapshot_redact.RedactionReport] | None":
    """Build a redacted archive to upload in place of *outfile*.

    Returns ``None`` only when redaction is deliberately DISABLED by config — the one case
    where sending the original is the intended behaviour. Every failure raises
    `RedactionFailed` instead, because "could not redact" must never fall through to
    "upload it unredacted": that would turn a broken bundle into a credential leak.

    The local bundle is never touched. It sits on the machine that already holds these
    secrets, so redacting it would destroy the only copy that restores complete and buy
    nothing — the boundary worth defending is the one the upload crosses.
    """
    try:
        redact = _redactor().outbound_redaction_enabled()
    except _redactor().RedactionSwitchUnreadable as e:
        # The operator wrote this file on purpose and we cannot tell which way. Neither
        # silent answer is honest -- off ignores a request to scrub, on rewrites files they
        # may not have meant to touch -- so refuse the UPLOAD and name the file. The local
        # bundle is already written and is unaffected.
        raise RedactionFailed(
            f"{e}.\n"
            "   Refusing to upload rather than guess whether your files should be "
            "rewritten. Fix the file (or delete it to leave redaction off), then re-run."
        ) from e
    if not redact:
        return None

    stage = workdir / "redacted"
    try:
        with tarfile.open(outfile) as tf:
            _refuse_oversized_archive(tf)
            try:
                tf.extractall(path=str(stage), filter=_data_filter)  # nosec B202
            except TypeError:
                # Python < 3.11.4 has no `filter` parameter, so the same manual member
                # screen the restore path uses applies here. Without it the keyword is an
                # uncaught TypeError -- not caught by the clause below, which lists only
                # archive and I/O failures -- so the off-host path crashed on those
                # interpreters while the local snapshot had already been written.
                members = [m for m in tf.getmembers() if _data_filter(m) is not None]
                tf.extractall(path=str(stage), members=members)  # nosec B202
    except (tarfile.TarError, OSError, EOFError, _ArchiveTooLarge) as e:
        raise RedactionFailed(f"could not read the bundle back to redact it ({e})") from e
    roots = [d for d in stage.iterdir() if d.is_dir()] if stage.is_dir() else []
    if len(roots) != 1:
        raise RedactionFailed(f"expected one bundle root to redact, found {len(roots)}")

    try:
        report = _redactor().redact_bundle_for_egress(roots[0])
    except _redactor().PayloadDatabaseUnprovable as e:
        shown = ", ".join(_safe_name(x) for x in sorted(e.details))
        raise RedactionFailed(
            f"the database this backup exists to carry cannot be shown free of "
            f"credentials: {shown}. It was NOT removed — uploading the remainder would "
            "report success and restore nothing. Your local snapshot is complete and "
            "unaffected. Re-run `kirocrew snapshot` once the database is readable, or "
            "turn the switch off in your backup directory to upload the bundle complete and "
            "unredacted"
        ) from e
    except _redactor().OpaqueFilesPresent as e:
        shown = ", ".join(_safe_name(p) for p in sorted(e.paths)[:10])
        more = "" if len(e.paths) <= 10 else f" (+{len(e.paths) - 10} more)"
        raise RedactionFailed(
            f"{len(e.paths)} file(s) are not text, so they cannot be shown free of "
            f"credentials: {shown}{more}. They were NOT removed — a restore that "
            "silently lacks your own files is worse than an upload that stops. Narrow "
            "the selection with --components, or turn redaction off in your backup directory to "
            "upload the bundle complete and unredacted"
        ) from e
    except (OSError, ValueError) as e:
        # Any failure inside the pass means the copy cannot be proven clean. Letting it
        # out as a traceback would be indistinguishable from a crash, and the branch that
        # decides what to upload would never run — so it becomes a refusal like the rest.
        raise RedactionFailed(f"the redaction pass failed ({e})") from e
    redacted = workdir / f"{outfile.stem}.redacted.tar.gz"
    try:
        with tarfile.open(redacted, "w:gz") as tf:
            tf.add(str(roots[0]), arcname=roots[0].name)
        # The workdir is locked to the owner before any child is created (see the caller),
        # and this archive is STREAMED -- routing it through atomic_write would mean holding
        # a multi-gigabyte bundle in memory to pass as `content`. So this is the re-assert,
        # not the protection.
        rd = str(redacted)
        platform_compat.restrict_to_owner(rd)  # lockdown-ok: re-assert, owner-only workdir
    except OSError as e:
        raise RedactionFailed(f"could not write the redacted archive ({e})") from e
    return redacted, report


def _report_redaction(report: "snapshot_redact.RedactionReport") -> None:
    """Say what left the host in what state, per path, so it can be judged not trusted.

    Every path here is BUNDLE-DERIVED -- a workspace filename the operator (or anything
    writing to their home) chose, or an archive member name. Printing one raw lets it
    repaint the very report the operator is reading to decide whether to trust the upload,
    so each goes through `_safe_name` at this single point rather than at each `print`.
    """
    print(f"🛡️  Redacted the outbound copy ({report.total} replacement(s)).")
    for rel, n in sorted(report.replacements.items()):
        print(f"     {_safe_name(rel)}: {n}")
    if report.dropped:
        shown = ", ".join(_safe_name(d) for d in sorted(report.dropped))
        print(f"     dropped entirely: {shown}")
    if report.skipped_unreadable:
        shown = ", ".join(_safe_name(s) for s in sorted(report.skipped_unreadable))
        print(f"     could not be proven clean, so removed: {shown}")
    print(
        "     The LOCAL archive is unredacted and still restores complete. Restoring the "
        "off-host copy gives you working memory with inert credentials — re-enter them."
    )


def prepare_redacted_copy(outfile: Path, workdir: Path, selected: list[str]) -> Path | None:
    """Produce a redacted copy of *outfile* for a caller that is about to send it off-host.

    Returns the redacted archive's path, or ``None`` when the operator has not opted in --
    in which case the caller sends *outfile* itself. Raises `RedactionFailed` when the
    pass cannot complete, because "could not redact" must never fall through to "send it
    unredacted".

    This is the seam the off-host path consumes. It deliberately knows nothing about a
    destination: the bucket, its hardening, the consent grant and the transport all belong
    to the AWS Control app, which owns one drive bucket per account and routes every call
    through the deploy engine's `run_aws` chokepoint. What is left here is the one thing
    that app does not do -- rewriting the bytes that leave -- and it is kept here because
    it is the snapshot format's own business, not the transport's.

    *workdir* must be a directory the caller controls and removes; the redacted copy is
    written inside it. The LOCAL bundle is never touched: it sits on the machine that
    already holds these secrets, so redacting it would destroy the only copy that restores
    complete and buy nothing.
    """
    _report_unresolved_payload(selected)
    prepared = _redacted_upload_copy(outfile, workdir)
    if prepared is None:
        _report_unredacted_upload()
        return None
    payload, report = prepared
    _report_redaction(report)
    return payload


def _estimate_selected_bytes(mc: Path, selected: list[str]) -> tuple[int, int]:
    """Bytes the *selected* components would stage, and how many entries refused.

    Scoped to the SELECTION, not the data home. The whole-home walk this replaces
    stat-ed every file under ``mc`` before staging, which had two consequences: the
    number described an archive nobody asked for whenever ``--components`` narrowed
    it, and a data home holding one entry this process may not stat -- a
    platform-protected key at the root, which no component declares -- ended the
    command with a traceback that ``--components`` could not route around, because
    the walk ran first.

    Overlapping trees (``memory`` names ``workspace/memory`` while ``workspace``
    names the whole tree) are counted ONCE, by the same ancestor collapse the staging
    pass makes: the estimate matches what staging writes, one refused entry in the
    overlap is reported once rather than twice, and the shared subtree is walked once.

    Deliberately does NOT apply ``_staging_ignore``'s exclusions. The estimate feeds
    one warning about how long this may take, and reading the ignore rules per
    directory to shave a sidecar off a size nobody acts on costs more than it buys;
    over-estimating is the safe direction for a 'this may be slow' notice.

    Symlinks are not followed and not counted, matching the staging walk, which skips
    them. The second return value is the number of entries REFUSED for permission, for
    a caller that wants to say so; every other error is raised, so a failing disk ends
    the command here exactly as it would during staging rather than being folded into
    a count that reads like a handful of protected paths.
    """
    total = 0
    unreadable = 0
    seen: set[str] = set()

    def _count(path: Path) -> None:
        nonlocal total, unreadable
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            # A component names files most homes do not have. Absent is not a refusal
            # and must not be reported as one.
            return
        except PermissionError:
            unreadable += 1
            return
        if _stat.S_ISREG(st.st_mode):
            total += st.st_size

    def _on_walk_error(exc: OSError) -> None:
        nonlocal unreadable
        # A tree a component names but this home does not have is the normal case, and
        # `os.walk` reports it here rather than raising -- counting it would report
        # 'unreadable' on a fresh install where nothing was refused.
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            return
        # Only the permission class is absorbed, matching the staging walks. Anything
        # else -- an EIO, a disconnected mount -- is raised out of the estimate rather
        # than folded into a count, because a number cannot say 'the storage is
        # failing' and the operator would read it as a handful of protected paths.
        if not isinstance(exc, PermissionError):
            raise exc
        unreadable += 1

    trees: list[str] = []
    for comp in selected:
        spec = COMPONENTS[comp]
        for f in spec.files:
            _count(mc / f)
        trees.extend(spec.trees)
    # A tree already covered by an ANCESTOR in the same selection is dropped, the same
    # collapse the staging pass makes. `seen` already stops a file's bytes being added
    # twice, but nothing deduped the REFUSALS: walking the overlap twice met one
    # refused directory twice and reported it as two. It also stops the shared subtree
    # being walked twice on a selection like `memory,workspace`.
    covered = set(trees)
    for tree in trees:
        if any(
            other != tree and PurePosixPath(tree).is_relative_to(PurePosixPath(other))
            for other in covered
        ):
            continue
        root = mc / tree
        # A tree ROOT that is a link is not walked. Staging refuses one outright
        # (`safe_tree_root`), but only later, so without this the estimate is the one
        # pass that follows it -- reading a tree the components never declared and
        # reporting its size as theirs.
        if pinned_fs.is_reparse_point(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):
            # Deeper reparse points are pruned by hand. `os.walk` declines a SYMLINK
            # directory on its own, but a Windows junction is not a symlink to it, so on
            # that platform it descends -- into whatever the junction names, which the
            # components never declared. Nothing but a size leaves this function, so the
            # exposure is a number, not bytes or names; it is still a walk of a tree the
            # operator did not select, and the staging pass refuses the same entry.
            # Edited in place, which is the contract `os.walk` offers for pruning.
            dirnames[:] = [d for d in dirnames if not pinned_fs.is_reparse_point(Path(dirpath) / d)]
            for fn in filenames:
                _count(Path(dirpath) / fn)
    return total, unreadable


# ── Snapshot ──────────────────────────────────────────────────────────────────


def _report_redacted_bundle(snap: Path) -> None:
    """Tell the operator up front that this bundle's credentials are inert.

    A redacted bundle is structurally sound — the databases open, the JSON parses, the
    trees are complete — so nothing downstream refuses it, and that is deliberate. What it
    is NOT is a bundle you can restore and walk away from: the fields that authenticate
    have been replaced. Saying so here is the difference between an operator who re-enters
    a token and one who spends an evening debugging a bot that will never connect.
    """
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    info = data.get("redaction") if isinstance(data, dict) else None
    if not isinstance(info, dict) or not info.get("redacted"):
        return

    print("🛡️  This bundle was REDACTED before it left its host.")
    reps = info.get("replacements")
    if isinstance(reps, dict) and reps:
        total = sum(v for v in reps.values() if isinstance(v, int))
        print(f"   {total} value(s) were replaced across {len(reps)} path(s):")
        for rel, n in sorted(reps.items())[:12]:
            # BOTH halves come out of a manifest this host did not write, so both are
            # escaped. Filtering non-integers while SUMMING does not make the count safe
            # to PRINT: a crafted value here is what repaints the report the operator is
            # reading to decide whether the upload can be trusted.
            print(f"     {_safe_name(rel)}: {_safe_name(str(n))}")
    dropped = info.get("dropped")
    if isinstance(dropped, list) and dropped:
        print(
            "   Left out entirely: " + ", ".join(_safe_name(d) for d in sorted(map(str, dropped)))
        )
    rebuild = info.get("indexes_needing_rebuild")
    if isinstance(rebuild, list) and rebuild:
        print(
            "   Search index(es) absent and will need rebuilding: "
            + ", ".join(_safe_name(d) for d in sorted(map(str, rebuild)))
        )
    print(
        "   Your memory and settings restore normally; anything that AUTHENTICATES does "
        "not. Re-enter those credentials after restoring."
    )


def _build_snapshot(
    mc: Path,
    out: Path,
    name: str,
    *,
    selected: list[str] | None = None,
    purpose: Purpose = Purpose.BACKUP,
    root_name: str | None = None,
    allow_unpinned: bool = False,
    skipped_out: list[dict[str, str]] | None = None,
) -> Path:
    """Stage the data home into a temporary tree and publish it as one tarball.

    Extracted from ``snapshot_main`` so the staging pass has a boundary a refusal can
    be contained at: everything in here either produces a finished archive or raises,
    and the caller turns a :class:`kiro_crew.pinned_fs.PinnedPathRefusal` into an exit
    code rather than a traceback.

    *selected* names the components to stage and *purpose* what the bundle is for; both
    are resolved by the caller so this function never has to decide policy. Staging is
    per-component rather than over one flat file table, which is what makes
    ``--components memory`` a complete memory backup instead of a subset of one.

    They DEFAULT rather than being required, because callers that predate the component
    seam ask for a whole-home archive and should keep getting one: an omitted *selected*
    means every component, which is what ``snapshot`` without ``--components`` has always
    produced. Making them mandatory broke those callers with a TypeError instead.

    *skipped_out*, when given, is extended with the same records this function writes to
    ``MANIFEST.json``'s ``skipped``. An out-parameter rather than a second return value
    on purpose: the return type is a ``Path`` that several tests use directly, so
    widening it would be a seam move with fallout, and the caller needs only to know
    WHETHER anything was omitted.

    *name* names the TARBALL and *root_name* the directory inside it, and they are two
    parameters rather than one because a selective bundle marks only the inner directory.
    Collapsing them renamed the tarball too, and ``--list``, pruning and ``--keep`` all
    glob ``kirocrew-snapshot-*.tar.gz`` -- so every partial bundle became invisible to
    rotation and accumulated without bound. Defaults to *name* for a complete bundle.
    """
    arcname = root_name or name
    if selected is None:
        selected = list(COMPONENTS)
    # Decided BEFORE anything is staged, not per-tree. An earlier revision gated
    # inside _copytree_safe only, so a data home with core files and no trees staged
    # them on a platform that cannot pin without ever consulting the opt-in -- the
    # gate was reachable only through a path that happened to exist. Asking once, up
    # front, is also what makes the manifest's "staging" value true of the whole
    # archive rather than of whichever component ran last.
    pinned = _staging_is_pinned(allow_unpinned=allow_unpinned, what="data home")

    # Every skip is recorded, not just printed. Printing alone leaves a snapshot that
    # omitted a hardlinked or symlinked file reporting success with a console warning
    # and nothing in the archive -- a silent partial. Paths are stored relative to the
    # data home so the record names the file without carrying the absolute layout of
    # the machine into an archive that may be moved somewhere else.
    skipped: list[dict[str, str]] = []

    def _record_skip(reason: str, path: str) -> None:
        try:
            rel = str(Path(path).relative_to(mc))
        except ValueError:
            rel = Path(path).name
        skipped.append({"reason": reason, "path": rel})
        _report_skip(reason, path)

    with tempfile.TemporaryDirectory() as work:
        # Locked down as a DIRECTORY before anything is staged into it. This tree holds the
        # operator's whole data home in the clear while the archive is built, and Windows
        # inherits the parent DACL rather than honouring a mode, so the POSIX 0700 mkdtemp
        # gives is not the guarantee on every platform this runs on.
        platform_compat.restrict_dir_to_owner(work)
        stage = Path(work) / arcname
        # Unconditionally, before any component runs. A file-only selection whose files
        # are all absent (a fresh home with `--components crons`) stages nothing, and the
        # manifest write below would then fail on a missing directory -- an empty bundle
        # is a valid outcome, a crash is not.
        #
        # Only the ROOT is created. A tree's own directory is created by the staging
        # primitive, which refuses a destination name that already exists in a tree it
        # made -- so pre-creating them here is what made the overlap collide.
        stage.mkdir(parents=True, exist_ok=True)

        # Core files. Copied through the pinned primitive rather than shutil.copy2:
        # copy2 dereferences a hardlink into ordinary-looking regular bytes, and the
        # tar pass's hardlink screen then has no link left to reject, so an alias
        # planted at a core file's name would have shipped as content. The name-based
        # islink check is gone with it -- it answered about a name, and the open that
        # followed could land on a different inode.
        #
        # Both ends are pinned where the platform allows it: the data home is opened
        # once and every core file is opened relative to THAT descriptor, so an
        # ancestor of the data home swapped mid-run cannot redirect the read. Opening
        # `mc / f` by name was a real gap in the first revision of this PR, caught in
        # review -- the file's own O_NOFOLLOW says nothing about the directories walked
        # to reach it.
        mc_fd = pinned_fs.open_dir_pinned(mc, what="data home") if pinned else None
        try:
            for comp in selected:
                for f in COMPONENTS[comp].files:
                    src = mc / f
                    if mc_fd is not None:
                        # Asked through the descriptor. `is_regular_at` lstats relative to
                        # mc_fd, so it rejects a link or a Windows junction by itself --
                        # a reparse point is not S_ISREG -- and there is no name for a
                        # concurrent swap to redirect.
                        #
                        # My own AST ratchet flagged this very line last round and I
                        # dismissed it as one of the legitimate by-name fallback sites
                        # without checking. It was not: this loop holds mc_fd. Review
                        # caught what I had waved off.
                        # A core file that simply is not there is not an omission and must
                        # stay out of MANIFEST.json -- most components ship only a subset.
                        # Only a name that EXISTS and is not a regular file is a skip worth
                        # recording, so the two cases are separated rather than collapsed
                        # into one `is_regular_at` call. Caught by the manifest test.
                        live_st = pinned_fs.stat_at(mc_fd, f)
                        if live_st is None:
                            continue
                        if not _stat.S_ISREG(live_st.st_mode):
                            _record_skip(pinned_fs.SKIP_NOT_REGULAR, str(src))
                            continue
                    else:
                        if not src.is_file():
                            continue
                        # Reserved for the fallback, where there is no descriptor to ask.
                        # `is_file()` and, on a platform without O_NOFOLLOW, `os.open`
                        # both FOLLOW a link, so neither can screen one: on the declared
                        # by-name path a core filename pointed at a credential would have
                        # had its bytes copied into the archive. `is_reparse_point` also
                        # catches a Windows junction, which `islink` does not report.
                        if pinned_fs.is_reparse_point(src):
                            _record_skip(pinned_fs.SKIP_SYMLINK, str(src))
                            continue
                    if f.endswith(".db"):
                        # A component file may sit under a subdirectory
                        # (`workspace/knowledge/knowledge.db`), so the parent is created
                        # per file rather than assumed from the component's tree roots.
                        (stage / f).parent.mkdir(parents=True, exist_ok=True)
                        # Through the SAME hardened copy the tree pass uses, so the two
                        # cannot drift: descriptor-verified chain, percent-escaped URI,
                        # `mode=ro`, and "not a database" told apart from "cannot read
                        # this database". Without it the core path is an unhardened
                        # SQLite read on the creation path, connecting READ-WRITE to
                        # the live name.
                        #
                        # `mc_fd` screened this name a few lines up and cannot be handed
                        # to SQLite, which takes only a path; the chain check inside the
                        # helper is what makes the ANCESTORS non-redirectable, and the
                        # residual final-name window is documented there.
                        # `require_database=True` makes every non-success outcome a raise,
                        # so there is no outcome to branch on here: a declared component
                        # file is either copied consistently or the snapshot fails. Both
                        # degradations it replaces -- byte-copying a corrupt database, and
                        # omitting an unverifiable one -- let the command succeed, which
                        # lets `--keep` prune the last good archive in favour of one that
                        # cannot be restored.
                        _copy_database_consistently(
                            src,
                            stage / f,
                            root=mc,
                            rel_parts=PurePosixPath(f).parts,
                            require_database=True,
                        )
                    elif mc_fd is not None:
                        (stage / f).parent.mkdir(parents=True, exist_ok=True)
                        pinned_fs.copy_file_pinned(
                            str(src),
                            str(stage / f),
                            dir_fd=mc_fd,
                            name=f,
                            on_skip=_record_skip,
                        )
                    else:
                        pinned_fs.copy_file_pinned(str(src), str(stage / f), on_skip=_record_skip)
        finally:
            if mc_fd is not None:
                os.close(mc_fd)

        # Trees. Selections overlap by design -- `memory` names workspace/memory while
        # `workspace` names the whole tree -- so the pairs are collected first and a tree
        # already covered by an ANCESTOR in the same selection is dropped.
        #
        # An earlier revision staged the overlap twice and relied on the second write being
        # identical to the first. That worked against a `shutil.copytree(dirs_exist_ok=True)`
        # and does NOT work here: the shared staging primitive refuses a destination name
        # that already exists in a tree this operation created, because it cannot tell a
        # directory it made from a link someone planted. Not copying the same bytes twice is
        # the better answer anyway.
        wanted_trees: list[tuple[str, str]] = []
        for comp in selected:
            for tree in COMPONENTS[comp].trees:
                # Guarded HERE, while collecting, rather than in the staging loop below.
                # An unsafe root must FAIL the snapshot, not be skipped: skipping produced
                # the worst possible artefact -- a bundle whose manifest declares `memory`
                # while the markdown trees are silently absent, so the operator believes
                # they are covered and only finds out when they try to recover. A backup
                # that lies about its contents is worse than no backup.
                #
                # safe_tree_root returns None only for an unsafe or unresolvable root -- a
                # root that simply does not exist yet is fine -- so this cannot fire on a
                # fresh data home.
                if safe_tree_root(mc / tree, what="component root", home=mc) is None:
                    raise UnsafeComponentRoot(
                        f"component {comp!r} names the tree {tree!r}, which does not "
                        f"resolve inside the data home. Refusing to write a bundle that "
                        f"would claim to contain {comp!r} without it -- inspect that path "
                        f"(it is usually a symlink) and re-run."
                    )
                wanted_trees.append((comp, tree))
        covered = {t for _, t in wanted_trees}
        staged_trees = [
            (comp, tree)
            for comp, tree in wanted_trees
            if not any(
                other != tree and PurePosixPath(tree).is_relative_to(PurePosixPath(other))
                for other in covered
            )
        ]
        for comp, tree in staged_trees:
            src_dir = mc / tree
            dst_dir = stage / tree
            # Classified by an explicit stat, because `Path.is_dir()` answers False for a
            # path it cannot stat -- so a tree ROOT the process may not reach took the very
            # same branch as a root that is simply absent, and an entire selected tree left
            # the bundle with nothing in `skipped`. The manifest then still declared the
            # component present, the retention guard below saw no omission, and `--keep`
            # pruned the last complete archive: silent, unbounded loss. A refusal here is
            # the same failure the per-entry screens already record -- only the loop's own
            # root probe had been left behind. `safe_tree_root` above does not foreclose
            # this: its `resolve(strict=False)` is best-effort and does not raise on a
            # refusal, so it returns a path and the root reaches this line unclassified.
            try:
                root_st: os.stat_result | None = os.stat(src_dir)
            except PermissionError:
                _record_skip(pinned_fs.SKIP_UNREADABLE_ENTRY, str(src_dir))
                continue
            except FileNotFoundError:
                # ONLY absent, which is ordinary on a fresh data home. Every other errno
                # propagates: `EIO` on a failing disk, `ESTALE` on a dropped NFS handle,
                # `ENOTCONN` on a disconnected mount, `ENOTDIR` on a data home whose
                # ancestor is a file. Folding those in here omitted a whole selected tree
                # with nothing in `skipped`, so the manifest still declared the component
                # present and the guard below pruned the last complete archive -- the same
                # silent loss this probe exists to stop, one errno class over. A backup
                # target is exactly where those errnos happen, so this is the last branch
                # that may hide them; the estimate already raises on the same class.
                root_st = None
            if root_st is None or not _stat.S_ISDIR(root_st.st_mode):
                continue
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            with ExitStack() as admission:
                if tree == MEMORY_STORES_DIR_NAME:
                    # The manifest copy and database backup must see the same generation.
                    admission.enter_context(hold_stores_for_read(src_dir))
                _copytree_safe(
                    src_dir,
                    dst_dir,
                    allow_unpinned=allow_unpinned,
                    on_skip=_record_skip,
                    ignore=_staging_ignore(tree, src_dir),
                    # `_record_skip` puts every one of these in MANIFEST.json, which
                    # is what makes tolerating them honest rather than silent.
                    skip_unreadable=True,
                )
                # Include WAL-resident rows through SQLite's backup API, without copying
                # sidecars that belong to the live database rather than this snapshot.
                _restage_databases(src_dir, dst_dir, bundle_root=stage, on_skip=_record_skip)

        # Manifest
        ws_files = sum(1 for _ in (stage / "workspace").rglob("*") if _.is_file())
        pm_files = sum(1 for _ in (stage / "plan_memory").rglob("*") if _.is_file())
        sk_dir = stage / "skills"
        sk_count = sum(1 for _ in sk_dir.iterdir() if _.is_dir()) if sk_dir.is_dir() else 0
        ms_dir = stage / MEMORY_STORES_DIR_NAME
        ms_count = sum(1 for _ in ms_dir.iterdir() if _.is_dir()) if ms_dir.is_dir() else 0
        # Recorded so a reader can tell how the archive was built. "unpinned" means
        # the trees were walked by name, which an ancestor swap during staging could
        # have redirected. Someone deciding whether to trust this archive needs that
        # on the record rather than in the memory of whoever ran the command.
        staging_mode = "pinned" if pinned else "unpinned"
        manifest = {
            "version": MANIFEST_VERSION,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hostname": socket.gethostname(),
            "user": os.environ.get("USER", "unknown"),
            "kirocrew_dir": str(mc),
            "purpose": purpose.value,
            # Which components rode, and what each declared about credential material.
            # A reader of the bundle can answer "is this safe to hand to someone"
            # from the manifest instead of inferring it from the file list.
            "components": {c: COMPONENTS[c].policy.value for c in selected},
            "staging": staging_mode,
            "skipped": skipped,
            "contents": {
                "memory_db": _fsize(stage / "memory.db"),
                "memory_index_db": _fsize(stage / "memory_index.db"),
                "crons_json": _fsize(stage / "crons.json"),
                "config_json": _fsize(stage / "config.json"),
                "notifications_jsonl": _fsize(stage / "notifications.jsonl"),
                "workspace_files": ws_files,
                "plan_memory_files": pm_files,
                "skill_count": sk_count,
                "memory_store_count": ms_count,
            },
        }
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if skipped_out is not None:
            # Taken from the same list the manifest just recorded, at the same point, so
            # the bundle and the caller cannot disagree about what was omitted.
            skipped_out.extend(skipped)
        if staging_mode == "unpinned":
            print(
                "⚠️  Staged by path name (--allow-unpinned-staging): this platform "
                "cannot pin a directory by descriptor, so an ancestor swapped during "
                "staging could have redirected a copy. Recorded in MANIFEST.json."
            )

        # Tarball — write to temp file and rename atomically to avoid corrupt partials
        out.mkdir(parents=True, exist_ok=True)
        outfile = out / f"{name}.tar.gz"
        tmp_tar = outfile.with_suffix(".tar.gz.tmp")
        try:
            with tarfile.open(str(tmp_tar), "w:gz") as tar:
                tar.add(str(stage), arcname=arcname, filter=_data_filter)
            # Lock the archive down BEFORE it is published.
            #
            # This tarball can contain sel_hmac.key, and the window between the rename
            # and a lockdown applied afterwards is not Windows-only: tarfile does not
            # create its file 0600, so on POSIX the archive is readable at its final,
            # predictable path until the chmod lands too.
            #
            # restrict_to_owner (fail-loud), NOT chmod_safe: chmod_safe swallows OSError
            # and would let the snapshot land group/world-readable while still printing
            # success. Failing here leaves the temp for the handler below to remove and
            # publishes nothing, which is what makes the "abort rather than ship an
            # under-protected archive" promise true by construction. POSIX applies chmod
            # 0o600; Windows applies an owner-only DACL in-process, and a
            # same-directory rename carries the explicit ACE with the file.
            platform_compat.restrict_to_owner(str(tmp_tar))
            tmp_tar.rename(outfile)
        except BaseException:
            tmp_tar.unlink(missing_ok=True)
            raise
    return outfile


def snapshot_main(
    argv: list[str] | None = None, *, parsed: argparse.Namespace | None = None
) -> int:
    if parsed is None:
        p = argparse.ArgumentParser(
            prog="kirocrew-snapshot",
            description="Create a portable .tar.gz snapshot of Kiro Crew state.",
        )
        p.add_argument("output_dir", nargs="?", default=_default_snapshot_dir())
        p.add_argument("--keep", type=int, default=7)
        p.add_argument("--list", action="store_true", dest="list_snapshots")
        p.add_argument(
            "--allow-unpinned-staging",
            action="store_true",
            dest="allow_unpinned",
            help=(
                "Stage by path name on a platform that cannot open a directory "
                "relative to a descriptor. Without this the snapshot is refused there "
                "rather than taken with a traversal an ancestor swap could redirect. "
                "The archive's MANIFEST.json records that it was staged unpinned."
            ),
        )
        p.add_argument("--components", default=None)
        p.add_argument("--purpose", default=Purpose.BACKUP.value)
        p.add_argument("--to", default=None, help=argparse.SUPPRESS)
        parsed = p.parse_args(argv)
    args = parsed
    allow_unpinned = bool(getattr(args, "allow_unpinned", False))

    if args.keep <= 0:
        print(f"❌ --keep value must be a positive integer, got: {args.keep}")
        return 1

    # `--to s3://…` never worked, and the off-host destination it was replaced by is now
    # the AWS Control app's. Kept as an explicit refusal rather than dropped, because
    # silently accepting it would write the bundle into a local directory named `s3:`.
    if getattr(args, "to", None):
        print(
            f"❌ --to is no longer accepted (you passed {args.to!r}).\n"
            f"   This command writes a local bundle. To keep a copy off-host, open the\n"
            f"   AWS Control app's Backup section, which owns the bucket and the push."
        )
        return 1

    out = Path(args.output_dir or _default_snapshot_dir())

    if args.list_snapshots:
        if not out.is_dir():
            print(f"No snapshots found in {out}")
            return 0
        snaps = sorted(
            out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True
        )
        for s in snaps:
            print(s)
        if not snaps:
            print(f"No snapshots found in {out}")
        return 0

    mc = _mc_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # Resolve the seam before doing any work: a refusal here must cost nothing and
    # must not leave a half-written bundle behind.
    try:
        purpose = Purpose(getattr(args, "purpose", None) or Purpose.BACKUP.value)
    except ValueError:
        print(
            f"❌ Unknown --purpose: {args.purpose} "
            f"(known: {', '.join(p.value for p in Purpose)})"
        )
        return 1
    supplied = getattr(args, "components", None)
    requested = [c.strip() for c in supplied.split(",") if c.strip()] if supplied else None
    if supplied and not requested:
        # `--components ,` parses to no names. Treating that as "no selection" is the
        # dangerous reading: it would produce a bundle carrying nothing but a manifest,
        # report success, and then `--keep` would count that empty bundle as the newest
        # backup and prune a real one. An explicit flag that names nothing is a mistake
        # in the invocation, so it fails before anything is written.
        print(
            f"❌ --components was given as {supplied!r}, which names no components.\n"
            "   Refusing rather than writing an empty bundle that retention would "
            "count as a backup.\n"
        )
        _list_components()
        return 1
    try:
        selected = resolve_components(requested, purpose)
    except ComponentRefused as e:
        print(f"❌ {e}")
        return 1

    # A SELECTIVE bundle gets a root directory name that older restores refuse.
    #
    # This is the one guard available against a hazard that cannot be fixed in the
    # consumer, because the consumer has already shipped: a released `kirocrew restore`
    # never reads the manifest's component map, and `_backup_and_copy` moves each live
    # core file out before checking whether the archive has a replacement. Point an old
    # restore at a memory-only bundle and it relocates `crons.json`, `config.json`, the
    # notifications store and the security files -- including `sel_hmac.key` -- into
    # `pre-restore-<ts>/`, then prints a tick for each one.
    #
    # What the released code DOES do is require the extracted root to start with
    # `kirocrew-snapshot-`, and print "Invalid snapshot format" and exit 1 otherwise --
    # before touching anything. So naming a partial bundle's root differently converts
    # silent data relocation into a clean refusal on every version already in the wild.
    #
    # The TARBALL keeps the familiar name: `--list`, pruning and `--keep` all glob
    # `kirocrew-snapshot-*.tar.gz`, and a partial bundle still needs to be found and
    # rotated by them. Only the directory inside it carries the marker.
    # Filled by the staging pass below, and read by the retention guard after it.
    omissions: list[dict[str, str]] = []
    complete = set(selected) == set(COMPONENTS)
    name = f"kirocrew-snapshot-{ts}"
    root_name = name if complete else f"kirocrew-partial-{ts}"

    # Pre-flight size estimate, over what this run will actually stage.
    if mc.is_dir():
        total_bytes, unreadable = _estimate_selected_bytes(mc, selected)
        total_mb = total_bytes / (1024 * 1024)
        if total_mb > 500:
            print(f"⚠️  {mc} is {total_mb:.0f} MB — snapshot may be large and slow")
        if unreadable:
            # stderr, and a count rather than a list: on a stock macOS install this is
            # a handful of platform-protected paths, and the operator's stdout carries
            # the bundle path. Each one that the STAGING pass also meets is named
            # individually and recorded in MANIFEST.json, so nothing is lost by
            # summarising here.
            print(
                f"⚠️  skipped {unreadable} unreadable entr"
                f"{'y' if unreadable == 1 else 'ies'} while estimating the size",
                file=sys.stderr,
            )

    # NO pre-staging WAL checkpoint, deliberately: it would be the ONLY write this command
    # makes to the live database, and it cannot be made safe. A checkpoint cannot run
    # read-only, so the name has to be reopened for writing -- and verifying the name first
    # does not close that, because SQLite re-resolves it, so a swap in the window between
    # the check and the open puts the checkpoint's WRITE into whatever the name then points
    # at, truncating an external database's log.
    #
    # Nothing the archive depends on is lost. The backup API reads a consistent snapshot
    # that already INCLUDES rows living only in the `-wal`: measured against a
    # cross-process writer with `wal_autocheckpoint=0` and a 1.5 MB log, the copy carried
    # all 421 rows -- 371 of them WAL-resident -- and passed `integrity_check`. The
    # checkpoint only kept the log from riding along at its full size, which is a size
    # optimisation, not a correctness step.
    #
    # With it gone, the whole creation path is read-only: every database is opened
    # `mode=ro`, so `kirocrew snapshot` cannot modify the data it was asked to copy.
    # That is a cleaner invariant than "read-only except one verified write", and it is
    # what makes `test_the_command_never_writes_to_the_live_database` assertable.

    try:
        outfile = _build_snapshot(
            mc,
            out,
            name,
            selected=selected,
            purpose=purpose,
            root_name=root_name,
            allow_unpinned=allow_unpinned,
            skipped_out=omissions,
        )
    except pinned_fs.PinnedPathRefusal as exc:
        # A refusal is a decision this command made on purpose. A traceback would
        # read like a crash and bury the sentence saying what to do about it.
        #
        # It is also a PERMISSION decision, so it belongs in the SEL log next to
        # `state_restore_rejected`. Review's point: the refusals this change introduced
        # returned without auditing, so the one outcome a reviewer would most want a
        # record of -- staging declined on an unsupported platform -- left no trace.
        _audit("snapshot_rejected", f"reason=unpinnable_staging detail={exc}")
        print(f"❌ {exc}")
        return 1
    except UnsafeComponentRoot as e:
        # Raised before the archive was published, so this is a clean refusal rather than
        # a crash. Reported as one, for the same reason as the refusal above.
        _audit("snapshot_rejected", f"reason=unsafe_component_root detail={e}")
        print(f"❌ {e}")
        return 1
    except DatabaseCopyFailed as e:
        # A database that could not be copied consistently means the bundle would restore
        # incomplete memory. Refusing is the only honest answer: a bundle reported as
        # created is a bundle the operator will rely on.
        _audit("snapshot_rejected", f"reason=database_copy_failed detail={e}")
        print(f"❌ {e}")
        print("   No bundle was written. Stop the gateway and re-run.")
        return 1

    sz = outfile.stat().st_size
    human = f"{sz // 1024}K" if sz < 1024 * 1024 else f"{sz / 1024 / 1024:.1f}M"

    # The bound belongs at CREATION, not only on the paths that move a bundle around. A
    # bundle past it cannot be restored by this tool, so reporting success would promise a
    # backup that does not exist -- and the prune below would then delete older bundles that
    # DO restore in favour of one that never will. Checked before both.
    try:
        with tarfile.open(outfile) as probe:
            _refuse_oversized_archive(probe)
    except _ArchiveTooLarge as e:
        print(f"❌ {e}.")
        print(
            f"   The archive is written at {outfile}, and nothing was pruned -- but this "
            "tool cannot restore it. Narrow it with --components, then delete this one."
        )
        _audit("snapshot_rejected", f"{outfile} ({human}): {e}")
        return 1
    except (tarfile.TarError, OSError, EOFError) as e:
        print(f"❌ The archive just written could not be read back ({e}).")
        print(f"   Left in place at {outfile}; nothing was pruned.")
        _audit("snapshot_rejected", f"{outfile} ({human}): unreadable: {e}")
        return 1

    print(f"✅ Snapshot created: {outfile} ({human})")

    _audit("snapshot_created", f"{outfile} ({human})")

    # Prune. This runs even when the upload failed, because --keep is a promise about
    # local disk and a persistently failing destination must not turn a daily backup
    # into an unbounded pile of bundles -- the disk fills, and then the snapshot that
    # would have worked cannot be written either.
    snaps = sorted(
        out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True
    )
    # A run that could not READ something it was asked to carry does not prune.
    #
    # Retention decides what to delete by mtime alone, so a bundle missing a file it was
    # asked for counts as the newest backup exactly like a whole one: with `--keep 1`
    # today's incomplete bundle deletes yesterday's complete one, reports success, and
    # the operator's last restorable copy is gone. Nothing downstream can recover it.
    #
    # Skipping the prune is the conservative direction and the cheap one. It keeps more
    # than `--keep` asked for, which costs disk; pruning would cost the backup. The next
    # run that reads everything prunes normally and the surplus goes away on its own, so
    # this cannot grow without bound while the refusal is transient. A refusal that is
    # NOT transient -- a permanently protected file -- holds the surplus, and that is the
    # right way round: it is also the case where the older complete bundle is the only
    # complete one the operator will ever have.
    # Asked as a CLASS, never by naming a reason code. A guard that names
    # `unreadable_entry` lets `too_large`, `vanished` and `identity_changed` past it
    # into the prune: a guard that enumerates codes is stale from the moment the next
    # code is added, and it goes on answering the old way until someone remembers to
    # come back here. `pinned_fs.omits_wanted_data` answers True for anything not
    # explicitly screened-by-design, so a new reason is incomplete-by-default and the
    # surplus bundle is the cost of forgetting.
    # Named apart from the estimate's `unreadable` COUNT earlier in this function: two
    # different things, and reusing the name shadowed an int with a list.
    omitted = [s for s in omissions if pinned_fs.omits_wanted_data(s["reason"])]
    if omitted:
        reasons = ", ".join(sorted({str(s["reason"]) for s in omitted}))
        print(
            f"⚠️  Not pruning: this bundle omits {len(omitted)} entr"
            f"{'y' if len(omitted) == 1 else 'ies'} it was asked to carry "
            f"({reasons}; see MANIFEST.json). Older bundles are kept so a complete "
            f"one is not replaced by an incomplete one."
        )
    else:
        for old in snaps[args.keep :]:
            old.unlink()
            print(f"🗑  Pruned: {_safe_name(old.name)}")

    remaining = len(list(out.glob("kirocrew-snapshot-*.tar.gz")))
    print(f"📦 Snapshots in {out}: {remaining} (keep={args.keep})")
    return 0


# ── Restore ───────────────────────────────────────────────────────────────────


def _copy_notifications(src_path: Path, dst_path: Path) -> None:
    """Install the snapshot's notification records, ordered against the live writer.

    Refuses outright where ``O_NOFOLLOW`` does not exist -- see below. The refusal is
    made HERE, before the executor is acquired and before anything is opened, because
    it is a question about the platform rather than about this source.

    The whole body runs on the dashboard's notification worker when one exists, so a
    row the gateway delivers during the restore cannot be ordered ahead of the
    archive's and dropped by the reader's positional cap. See
    :func:`_serialise_with_notification_writes` for why ``O_APPEND`` alone was not
    enough, and note the cost: a restore briefly blocks notification writes. A
    restore is rare and user-initiated; a lost notification is silent and permanent.

    WHY A MISSING ``O_NOFOLLOW`` REFUSES INSTEAD OF FALLING BACK. The predecessor
    checked ``pinned_fs.is_reparse_point(src_path)`` by NAME and then opened the same
    NAME. Against an adversary that is not a floor, it is a check-to-open window: the
    threat model this function is written for -- stated in ``_install_notifications``
    and demonstrated by the revision review blocked -- is a concurrent agent replacing
    the extracted file, and such an agent chooses the timing. The descriptor check does
    not save it either: ``fstat`` asserts ``S_ISREG``, and a reparse point resolving to
    a regular ``.env`` passes ``S_ISREG``. So on a platform with no ``O_NOFOLLOW`` there
    is no sequence of by-name checks that makes this safe, and the honest move is to not
    do it.

    The alternative -- a ctypes ``CreateFileW`` with ``FILE_FLAG_OPEN_REPARSE_POINT`` --
    is declined, and not by me: ``eval/bench/safepath.py`` records that it "is no longer
    worth considering here: it would buy the same property exclusive creation already
    has, at the price of security code that cannot be exercised on the machine this
    harness is developed on", and ``skill_trust.py`` records that Python "does not expose
    an equivalent handle-relative, no-reparse walk on Windows". Both decline the capable
    route, which means this repo has already chosen less capability on that platform over
    a hand-rolled walk. Refusing extends those two decisions; falling back contradicts
    them.

    The cost is not symmetric, which is what makes the trade easy. Refusing loses
    notification HISTORY on one platform for one operation -- the records remain in the
    snapshot and nothing is destroyed. Falling back can put attacker-chosen bytes, a
    credentials file among them, into a location the agent then reads. And this PR exists
    because snapshot restore installed unvalidated bytes: a remaining path that installs
    attacker-chosen bytes is the same defect, closed everywhere except where it is
    hardest.

    The refusal is LOUD and the callers report the skip. A silent skip would be the same
    class of bug as the one being fixed, so the message names the platform, the missing
    primitive, and what was not imported.
    """
    if not getattr(os, "O_NOFOLLOW", 0):
        raise NotificationCopyUnsupported(
            f"this platform ({os.name}) has no O_NOFOLLOW, so the notification source "
            "cannot be opened with any guarantee that the name checked is the file "
            "read -- a by-name reparse-point check followed by a by-name open is a "
            "window a concurrent writer chooses the timing of, and fstat's S_ISREG "
            "does not close it because a reparse point to a regular file passes it. "
            "Refusing rather than importing bytes that may not be the archive's: "
            f"{_safe_name(src_path)} was NOT imported and remains in the snapshot"
        )
    _serialise_with_notification_writes(lambda: _install_notifications(src_path, dst_path))


def _do_merge(
    snap: Path, mc: Path, components: list[str] | None, *, allow_unpinned: bool = False
) -> None:
    # BOTH restore modes refuse an unsafe destination tree root up front, and merge needs
    # it for the same reason replace does: a merge that silently omits a tree is still a
    # merge that claims to have imported it. Skipping the tree and returning 0 is the worst
    # available outcome -- the operator is told the import succeeded while the notes they
    # were importing are not there.
    # REPLACE ONLY, from the shape of the data: an artifact is a DIRECTORY whose files
    # describe each other, and a slug comes from the artifact's NAME, so a per-file
    # no-overwrite merge either tops one artifact up out of another's generation or needs a
    # rule for when two artifacts are the same artifact. Replace needs neither.
    #
    # Refused when that is all the operator asked for, skipped with a notice otherwise: a
    # run that imports nothing and reports success is the failure this component removes.
    replace_only = [c for c in _REPLACE_ONLY_COMPONENTS if _want(components, c)]
    if replace_only and components is not None and not set(components) - set(replace_only):
        raise ComponentRefused(
            f"component(s) {', '.join(replace_only)} are restored with --mode replace "
            "only; re-run with that mode."
        )
    # Scoped to the components this MODE writes. Refusing over a replace-only root aborts
    # an import that never reaches it -- a home keeping `uploads/` on another disk behind a
    # link could not merge its memory at all. Never narrows to nothing: a selection of only
    # those was refused above.
    merged_components = [
        c for c in COMPONENTS if _want(components, c) and c not in _REPLACE_ONLY_COMPONENTS
    ]
    _refuse_unsafe_destination_roots(mc, merged_components)
    # Asked once, at entry, BEFORE any mutation. The core-file copies below run before
    # any tree call, so gating inside the tree helpers meant a merge on a platform that
    # cannot pin wrote memory.db, crons.json and the security files first and only then
    # met the refusal -- either redirecting those writes through a planted link, or
    # aborting with the restore already half applied. Review caught it; it is the same
    # gate-placement defect as the snapshot side, one path over.
    _staging_is_pinned(allow_unpinned=allow_unpinned, what="merge restore")
    print("🔀 Merge mode — importing...")

    if _want(components, "memory") and (snap / "memory.db").is_file():
        if not (mc / "memory.db").is_file():
            shutil.copy2(str(snap / "memory.db"), str(mc / "memory.db"))
            if (snap / "memory_index.db").is_file():
                shutil.copy2(str(snap / "memory_index.db"), str(mc / "memory_index.db"))
            print("  Memory: copied (no existing memory.db)")
        else:
            _merge_memory(snap / "memory.db", mc / "memory.db")
        print("  ✅ memory")

    # The markdown half of memory (preferences, projects, history, knowledge). Named
    # by the memory component so restoring memory does not require the whole
    # workspace; no-overwrite so a merge never clobbers newer local files.
    if _want(components, "memory"):
        for tree in COMPONENTS["memory"].trees:
            sd = snap / tree
            if sd.is_dir():
                dd = mc / tree
                # RE-CHECKED here, not only in the pre-flight at the top of this function.
                # The pre-flight clears the same set moments earlier, so a root that fails
                # now failed AFTER it -- something moved under us mid-run. Replace already
                # refuses that; merge did not, and the gap was reachable: with `workspace`
                # swapped for an external link straight after the pre-flight, this loop's
                # `mkdir(parents=True)` created the tree THROUGH the link and the copy wrote
                # the operator's memory files into an attacker-chosen directory outside the
                # data home -- four of them, with the run printing "Merge complete."
                #
                # The per-file screens cannot see it: each final component is a fresh regular
                # file, and a by-name open does not check its ancestors. Refusing on the root
                # is what closes it. Merge is additive, so a component already merged stays
                # merged -- nothing is destroyed by stopping here, unlike carrying on.
                if safe_tree_root(dd, what="destination root", home=mc) is None:
                    raise UnsafeComponentRoot(
                        f"memory:{tree} stopped resolving inside the data home after the "
                        "pre-flight check passed. A path that was safe moments ago and is "
                        "not now was replaced mid-run (usually a symlink); merging past it "
                        "would write this component outside the data home."
                    )
                if tree == MEMORY_STORES_DIR_NAME:
                    kept = _merge_named_stores(sd, dd, allow_unpinned=allow_unpinned)
                    for store in kept:
                        print(
                            f"  ↩️  {tree}/{_safe_name(store)}: kept the existing store; "
                            "the bundle's copy was NOT merged into it. "
                            "To take the bundle's copy instead, use --mode replace."
                        )
                    continue
                dd.mkdir(parents=True, exist_ok=True)
                _report_unmerged_databases(sd, dd, tree)
                _copy_tree_no_overwrite(sd, dd, allow_unpinned=allow_unpinned)

    if _want(components, "crons"):
        sc, dc = snap / "crons.json", mc / "crons.json"
        crons_ok = True
        if sc.is_file():
            if dc.is_file():
                crons_ok = _merge_crons(sc, dc)
            else:
                shutil.copy2(str(sc), str(dc))
                print("  Crons: copied (no existing crons)")
        if crons_ok:
            print("  ✅ crons")
        else:
            print("  ⚠️  crons: merge skipped (see warning above) — no jobs imported")

    if _want(components, "config"):
        for f in CORE_FILES["config"]:
            s, d = snap / f, mc / f
            if s.is_file() and not d.is_file():
                shutil.copy2(str(s), str(d))
                print(f"  {f}: restored (was missing)")
        print("  ✅ config")

    if _want(components, "notifications"):
        sn, dn = snap / "notifications.jsonl", mc / "notifications.jsonl"
        if sn.is_file():
            if dn.is_file():
                _merge_notifications(sn, dn)
            else:
                # Not `copy2`: a byte-exact copy installs records the live file's
                # own reader refuses, and that reader loses the whole file to one
                # of them. Same abort posture as the merge branch above.
                #
                # The platform refusal is NOT that abort: it says this platform can
                # never do this safely, not that this archive is bad, so it skips one
                # component loudly and lets the rest of the restore proceed. Reported
                # rather than swallowed -- a silent skip is the bug class being fixed.
                try:
                    _copy_notifications(sn, dn)
                    print("  Notifications: copied")
                except NotificationCopyUnsupported as exc:
                    print(f"  ⚠️  Notifications: SKIPPED -- {exc}")
        print("  ✅ notifications")

    if _want(components, "security"):
        for f in CORE_FILES["security"]:
            s, d = snap / f, mc / f
            if s.is_file() and not d.is_file():
                if _copy_locked(s, d):
                    print(f"  {f}: restored (was missing)")
        print("  ✅ security")

    for comp in _WHOLE_TREE_COMPONENTS:
        if not _want(components, comp):
            continue
        if comp in _REPLACE_ONLY_COMPONENTS:
            # Named, not passed over in silence, and BEFORE the tick below so a component
            # this run did not import never reports one.
            print(
                f"  ⚠️  {comp}: SKIPPED -- restored with --mode replace only, which swaps "
                "the tree whole and keeps the previous one in the pre-restore backup."
            )
            continue
        for dirname in COMPONENTS[comp].trees:
            sd = snap / dirname
            if dirname in _LOCKED_DOCUMENT_TREES:
                if (snap / _LOCKED_DOCUMENT_TREES[dirname]).is_file():
                    _install_locked_document(dirname, snap, mc, only_if_absent=True)
                continue
            if sd.is_dir():
                dd = mc / dirname
                dd.mkdir(parents=True, exist_ok=True)
                _copy_tree_no_overwrite(sd, dd, allow_unpinned=allow_unpinned)
        print(f"  ✅ {comp}")

    print("✅ Merge complete.")


def _is_gateway_running() -> bool:
    """Check if the KiroCrew gateway is listening on its dashboard port."""
    # Deterministic override (used by tests / scripted restores) — avoids a real
    # socket probe whose result is environment-dependent.
    override = os.environ.get("KIROCREW_ASSUME_GATEWAY_RUNNING")
    if override is not None:
        return override.strip().lower() not in ("", "0", "false", "no")
    port = _DASHBOARD_PORT
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def restore_main(argv: list[str] | None = None, *, parsed: argparse.Namespace | None = None) -> int:
    if parsed is None:
        p = argparse.ArgumentParser(
            prog="kirocrew-restore", description="Restore KiroCrew state from a snapshot."
        )
        p.add_argument("snapshot", nargs="?")
        p.add_argument("--mode", choices=("replace", "merge"))
        p.add_argument("--dry-run", action="store_true")
        p.add_argument(
            "--force", action="store_true", help="Allow restore even if gateway is running"
        )
        p.add_argument("--components")
        p.add_argument("--list-components", action="store_true")
        p.add_argument(
            "--allow-unpinned-staging",
            action="store_true",
            dest="allow_unpinned",
            help=(
                "Restore by path name on a platform that cannot open a directory "
                "relative to a descriptor. Without this the restore is refused there "
                "rather than run with a destination an ancestor swap could redirect."
            ),
        )
        parsed = p.parse_args(argv)
    args = parsed
    allow_unpinned = bool(getattr(args, "allow_unpinned", False))

    if args.list_components:
        _list_components()
        return 0

    if not args.snapshot:
        print("❌ snapshot file is required (unless --list-components is given)")
        return 1

    force = getattr(args, "force", False)
    if not force and _is_gateway_running():
        _audit("state_restore_rejected", "reason=gateway_running")
        print("❌ Gateway is running. Stop it first (kirocrew stop) or use --force.")
        return 1

    # An s3:// argument is refused, not fetched: the drive bucket, the consent grant and
    # the transport are the AWS Control app's, and its restore deliberately lands the
    # archive in a staging folder rather than hot-swapping live state. Everything below
    # therefore operates on a LOCAL path -- and still treats it as untrusted input, since
    # a bundle that arrived from object storage is untrusted regardless of whose bucket it
    # came from. The extraction filter, the archive bound and the source-database
    # integrity refusal are that validation, and all three run BEFORE any live state
    # moves; the destination integrity check further down reports on the result and is not
    # what makes a bundle safe to apply.
    if str(args.snapshot).startswith("s3://"):
        # Fetching is the AWS Control app's job now: it owns the drive bucket, the
        # consent grant and the transport, and its restore deliberately lands the
        # archive in a staging folder rather than hot-swapping live state. This command
        # then restores from that local path. Refused explicitly rather than treated as
        # a filename, which would look for a directory named `s3:`.
        #
        # Escaped before printing: this is caller-supplied text on its way to a terminal.
        print(
            f"❌ Cannot fetch {_safe_name(str(args.snapshot))} directly.\n"
            "   Download it from the AWS Control app's Backup section first, then pass\n"
            "   the local path to this command."
        )
        return 1

    snap_path = Path(args.snapshot)
    if not snap_path.is_file():
        print(f"❌ File not found: {snap_path}")
        return 1

    # Parse components
    components: list[str] | None = None
    if args.components:
        requested = [c.strip() for c in args.components.split(",") if c.strip()]
        if not requested:
            # Same reasoning as the snapshot side: an explicit flag that names nothing
            # is an invocation mistake. Reading it as "restore no components" would
            # print success while touching nothing, which is worse than refusing.
            print(
                f"❌ --components was given as {args.components!r}, which names no "
                "components. Refusing rather than reporting a restore that did "
                "nothing.\n"
            )
            _list_components()
            return 1
        # Restore reads whatever the bundle holds, so the purpose gate does not apply
        # here — only the unknown-name refusal does.
        try:
            components = resolve_components(requested, Purpose.BACKUP)
        except ComponentRefused as e:
            print(f"❌ {e}\n")
            _list_components()
            return 1

    mc = _mc_dir()
    mode = args.mode or ("merge" if (mc / "memory.db").is_file() else "replace")

    with tempfile.TemporaryDirectory() as work_str:
        work = Path(work_str)
        # Same reasoning as the staging side: the extracted bundle sits here in the clear
        # before it is installed, so the directory is locked to the owner cross-platform
        # before any member is written into it.
        platform_compat.restrict_dir_to_owner(work)

        # Security checks are enforced inside _data_filter (no TOCTOU gap)
        #
        # Listing an archive and extracting it are different operations, so the download
        # probe passing does not mean this will: conflicting members (a file and a
        # directory claiming one name) or a stream that ends mid-member raise here, not
        # there. A refusal has to read as a refusal — every other rejection on this path
        # reports and exits 1 rather than surfacing a traceback.
        try:
            with tarfile.open(str(snap_path), "r:gz") as tar:
                # The bound belongs on EVERY path that reads an archive, not just the
                # ones that crossed a network. A local bundle can be hostile or simply
                # wrong, and on Python < 3.11.4 the fallback below calls `getmembers()`,
                # which materialises every entry — so an archive declaring millions of
                # them exhausts memory before a single file is written.
                _refuse_oversized_archive(tar)
                rejected_entries: list[str] = []
                _filter = _rejection_recording_filter(rejected_entries)
                try:
                    tar.extractall(work, filter=_filter)
                except TypeError:
                    # Python < 3.11.4: filter param not supported, apply manually
                    members = [m for m in tar.getmembers() if _filter(m) is not None]
                    tar.extractall(work, members=members)
                if rejected_entries:
                    # A bundle whose entries were dropped is not the bundle its manifest
                    # describes, and replace CLEARS live state for a component before asking
                    # whether the archive can refill it. Reproduced: an archive holding the
                    # memory tree as a link has that entry rejected, the staged tree is then
                    # absent, the live tree is cleared unconditionally, and the command reports
                    # success. Refusing here rather than in the mutation phase because this is
                    # the only layer that can tell a rejected entry from an archive that never
                    # carried it -- by then the two are the same state.
                    print(
                        "❌ This archive contains "
                        f"{len(rejected_entries)} entr{'ies' if len(rejected_entries) > 1 else 'y'} "
                        "that cannot be extracted safely "
                        f"({', '.join(sorted(rejected_entries)[:3])}"
                        f"{', ...' if len(rejected_entries) > 3 else ''}). Restoring it would "
                        "apply an incomplete bundle and, in replace mode, clear live state the "
                        "archive cannot put back.\n   Nothing was restored."
                    )
                    _audit(
                        "state_restore_rejected",
                        f"reason=unsafe_archive_entries from={snap_path.name}",
                    )
                    return 1
        except _ArchiveTooLarge as e:
            _audit(
                "state_restore_rejected",
                f"reason=archive_too_large from={snap_path.name}",
            )
            print(f"❌ {e}.\n   Nothing was restored.")
            return 1
        except (tarfile.TarError, OSError, EOFError) as e:
            _audit(
                "state_restore_rejected",
                f"reason=extraction_failed from={snap_path.name}",
            )
            print(f"❌ This snapshot could not be extracted ({e}).\n   Nothing was restored.")
            return 1

        # Both roots: `kirocrew-snapshot-` for a complete bundle and
        # `kirocrew-partial-` for a selective one. The second name exists so that
        # released versions, which require the first, refuse a partial bundle instead of
        # relocating the components it does not carry. This version reads the manifest,
        # so it can consume either.
        snap_dirs = [
            d
            for d in work.iterdir()
            if d.is_dir()
            and (d.name.startswith("kirocrew-snapshot-") or d.name.startswith("kirocrew-partial-"))
        ]
        if not snap_dirs:
            print("❌ Invalid snapshot format")
            return 1
        if len(snap_dirs) > 1:
            # Picking the first was arbitrary: two roots in one archive means the
            # selection about to drive `replace` is a coin toss, and replace deletes.
            print(
                "❌ This archive contains more than one snapshot root "
                f"({', '.join(sorted(_safe_name(d.name) for d in snap_dirs))}). Refusing rather "
                "than guessing which one to restore."
            )
            _audit("state_restore_rejected", f"reason=multiple_roots from={snap_path.name}")
            return 1
        snap = snap_dirs[0]
        partial_root = snap.name.startswith("kirocrew-partial-")

        _print_manifest(snap)
        _report_redacted_bundle(snap)
        try:
            declared = _manifest_components(snap)
        except ManifestUnreadable as e:
            print(f"❌ {e}")
            print(
                "   Refusing to guess what this bundle contains. A manifest this "
                "version cannot parse may mean a corrupt archive, so an explicit "
                "--components does not override it."
            )
            _audit("state_restore_rejected", f"reason=manifest_unreadable from={snap_path.name}")
            return 1
        if partial_root and declared is None and components is None:
            # The root name ASSERTS the bundle is selective, and the manifest is what
            # says which components it carries. A partial root with no component map is
            # a contradiction, and resolving it the permissive way is the worst option:
            # `declared is None` falls through to all-components below, so replace mode
            # would displace live components this bundle never held while reporting
            # success. Only a COMPLETE bundle may omit the map (pre-v3 archives did,
            # and for them all-components is correct because they held everything).
            #
            # Gated on `components is None` because an explicit selection is
            # checked against the bundle's actual contents below instead. Naming
            # the components is not on its own evidence the bundle holds them, so
            # the escape hatch this message offers is honoured by that check, not
            # by trusting the operator's list.
            print(
                "❌ This archive is marked partial but carries no component map, so "
                "there is no way to tell what it holds.\n"
                "   Refusing: restoring it as if it were complete would move live "
                "components it never contained.\n"
                "   Pass --components explicitly if you know what it carries."
            )
            _audit(
                "state_restore_rejected",
                f"reason=partial_without_manifest from={snap_path.name}",
            )
            return 1
        if components is None:
            # A selective bundle must not be restored as if it held everything. With
            # components unset, _want() answers True for every component, so a
            # memory-only bundle taken through `--mode replace` would rmtree the live
            # workspace and put back only the memory subtrees it carries — deleting
            # unrelated state the bundle never had.
            #
            # The manifest records what actually rode (v3+), so that is the default,
            # INCLUDING when it resolves to an empty set. A pre-v3 bundle has no map
            # (declared is None) and keeps the old all-components behaviour, which is
            # correct for it — it did hold everything.
            if declared is not None:
                # A DECLARED component the bundle carries no payload for is refused, for the
                # same reason the explicit-selection branch below refuses one: replace clears
                # live state for a component and then has nothing to put back. The two
                # branches had different answers to the same question, and only the explicit
                # one was guarded.
                #
                # Reproduced, and the product itself writes the bundle: a snapshot of a home
                # with no memory payload declares `memory` anyway, and restoring it with
                # replace onto a home that HAS memory cleared the memory trees and removed the
                # derived index while `memory.db` survived -- the database kept, the notes
                # indexed against it gone. A partial erasure is worse than either extreme.
                #
                # A DERIVED index does not count as payload: a bundle carrying only
                # `memory_index.db` still has no memory to restore, and treating it as payload
                # would let exactly the reproduced case through.
                #
                # Scoped to the component whose replace clears UNCONDITIONALLY, which is
                # the premise this message states. `artifacts` and `uploads` are legitimately
                # empty on a home that never made one, so an ordinary bundle declares them
                # with no payload; refusing on the declaration alone refused the entire
                # restore over two components that would have touched nothing. The
                # explicit-selection branch below still reports any hollow component, because
                # there the operator NAMED it and a silent no-op would answer a request with
                # nothing.
                hollow = [
                    c
                    for c in declared
                    # `memory` is the one component whose replace clears live state even
                    # when the bundle carries nothing for it: its tree loop clears
                    # unconditionally and a derived index the archive lacks is moved aside.
                    # Every other component mutates only what the bundle actually carries,
                    # so a hollow declaration there is a restore that does nothing.
                    if c == "memory" and _component_payload_absent(snap, c)
                ]
                if hollow:
                    print(
                        "❌ This bundle declares "
                        f"{', '.join(sorted(hollow))} but carries no data for "
                        f"{'them' if len(hollow) > 1 else 'it'}. Restoring would clear the "
                        "live state for those components with nothing to put back. Refusing "
                        "rather than partially erasing what is there."
                    )
                    _audit(
                        "state_restore_rejected",
                        f"reason=declared_without_payload from={snap_path.name}",
                    )
                    return 1
                components = declared
                print(
                    "🔧 Components (from bundle manifest): "
                    f"{','.join(components) if components else '(none)'}"
                )
        elif declared is not None:
            # An explicit selection the bundle does not contain is a refusal, not a
            # no-op: replace mode would move the live files of that component out to
            # the rollback dir and have nothing to put back.
            # Membership in the declaration is NOT enough, and testing only that is what left
            # the destructive case reachable from this side: a bundle can declare `memory` and
            # carry none of it, so `--components memory` passed a guard written for exactly
            # this situation. Reproduced -- the live memory trees were cleared and the command
            # then failed for an unrelated reason, so even the exit code did not give it away.
            # Both branches ask the same question now; guarding one of them with a stronger
            # test than the other is the whole defect.
            absent = [
                c for c in components if c not in declared or _component_payload_absent(snap, c)
            ]
            if absent:
                print(
                    f"❌ This bundle does not contain: {', '.join(sorted(absent))}\n"
                    f"   It carries: {', '.join(declared) if declared else '(nothing)'}"
                )
                return 1
        elif partial_root:
            # A partial root with no component map, which the guard above lets
            # through so an operator who knows the contents can name them. What
            # they named still has to be there: the same refusal as the branch
            # above, decided by what the archive holds because there is no map to
            # decide it from.
            absent = _components_absent_from_bundle(snap, components)
            if absent:
                print(
                    f"❌ This bundle does not contain: {', '.join(sorted(absent))}\n"
                    "   Its manifest carries no component map, so this is read from "
                    "the archive's contents.\n"
                    "   Refusing: replace mode would move that component's live files "
                    "aside with nothing to put back."
                )
                _audit(
                    "state_restore_rejected",
                    f"reason=named_component_absent from={snap_path.name}",
                )
                return 1
            # The component is carried, but "carried" is any ONE declared path, and
            # replace clears a component's directories before it knows whether the
            # archive has a replacement. So a bundle holding just `memory.db` satisfied
            # the check above while `workspace/memory` was absent, and replace cleared it
            # from live state and reported success.
            #
            # Only replace, and only a tree live state actually HAS. Merge clears nothing,
            # so the hatch stays usable there; and a tree live state lacks has nothing to
            # lose, which is what keeps this from refusing a sound bundle taken from a
            # home that never used that tree.
            if mode == "replace":
                absent_trees = _trees_absent_from_bundle(snap, components, mc)
                if absent_trees:
                    print(
                        "❌ This bundle carries no component map and is missing "
                        f"{', '.join(absent_trees)}, which live state HAS.\n"
                        "   Refusing: --mode replace clears a component's directories "
                        "before copying, so this would delete live state the archive "
                        "cannot put back. Naming the component does not establish that "
                        "the archive holds every part of it.\n"
                        "   Use --mode merge, which clears nothing, or a bundle that "
                        "carries a component map."
                    )
                    _audit(
                        "state_restore_rejected",
                        f"reason=partial_replace_absent_tree from={snap_path.name}",
                    )
                    return 1
        if components:
            print(f"🔧 Components: {','.join(components)}")

        if args.dry_run:
            print(f"\n🔍 Dry run — would restore to {mc} in {mode} mode")
            print("Files in snapshot:")
            for f in sorted(snap.rglob("*")):
                if f.is_file():
                    # Archive-derived, and a dry run is exactly when the operator is
                    # reading the list to decide whether to proceed.
                    print(f"  {_safe_name(f.relative_to(snap).as_posix())}")
            return 0

        mc.mkdir(parents=True, exist_ok=True)
        try:
            # Replace installs everything it carries; merge installs only what the
            # destination is missing. Both are validated for exactly what they will put in
            # place, so neither mode can install a database it never checked.
            _refuse_corrupt_source_databases(
                snap,
                components,
                mc_for_merge=None if mode == "replace" else mc,
            )
        except SourceComponentUnsound as e:
            _audit(
                "state_restore_rejected",
                f"reason=source_integrity_check_failed from={snap_path.name}",
            )
            print(f"❌ {e}")
            return 1
        # Contained here rather than allowed to propagate: a refusal is a decision
        # this command made on purpose, and a traceback would read like a crash and
        # bury the one sentence saying what to do about it.
        try:
            if mode == "replace":
                _do_replace(snap, mc, components, allow_unpinned=allow_unpinned)
            else:
                _do_merge(snap, mc, components, allow_unpinned=allow_unpinned)
        except pinned_fs.PinnedPathRefusal as exc:
            # Same reasoning as the snapshot handler, and this one reuses the event name
            # already established for a declined restore rather than inventing a second.
            _audit("state_restore_rejected", f"reason=unpinnable_staging detail={exc}")
            print(f"❌ {exc}")
            return 1
        except UnsafeComponentRoot as e:
            # Raised before anything was written, so this is a clean refusal. Report it
            # as one rather than letting a traceback out -- the same contract every other
            # refusal on this path already follows.
            print(f"❌ {e}")
            return 1
        except ComponentRefused as e:
            # A mode that cannot restore what was asked for, raised at the top of the merge
            # before any mutation. Same clean-refusal contract as the branch above; the
            # message names the mode that can.
            _audit("state_restore_rejected", f"reason=mode_unsupported detail={e}")
            print(f"❌ {e}")
            return 1
        except NamedStoresInUse as e:
            # Also before any mutation: a store open elsewhere would keep writing into an
            # unlinked database after the replace. The gateway check at the top catches the
            # ordinary case; this is the one `--force` and a second process can reach.
            _audit("state_restore_rejected", f"reason=named_store_in_use from={snap_path.name}")
            print(f"❌ {e}")
            return 1
        except UnreadableRecord as exc:
            # The notification merge aborts on a record it cannot deliver intact, so
            # that a partial copy is never reported as a success -- see
            # `_merge_notifications`. That refusal is as deliberate as the two above and
            # belongs in this list; while it was missing, it left this command as a
            # TRACEBACK, which tells the operator their tool broke rather than that their
            # data was rejected. Deliberately narrower than `(OSError, UnreadableRecord)`,
            # which is what the merge itself catches: an `OSError` here could come from
            # any copy in the restore, and labelling one of those a refused notification
            # record would be a wrong message rather than a missing one.
            _audit("state_restore_rejected", f"reason=unreadable_notification_record detail={exc}")
            print(f"❌ {exc}")
            return 1
        except SourceComponentUnsound as e:
            # `_allocate_rollback_dir` raises this when every candidate name for the current
            # timestamp is taken. Rare, and still a refusal rather than a crash: it happens
            # before any mutation, so the data home is untouched and the operator needs a
            # sentence rather than a stack trace. The pre-flight validator raises the same
            # type and is caught above; this handler covers the execution boundary, which
            # had no catch for it at all.
            _audit(
                "state_restore_rejected",
                f"reason=rollback_dir_unavailable from={snap_path.name}",
            )
            print(f"❌ {e}")
            return 1
        except RollbackIncomplete as e:
            # Said first and said plainly: the operator's next action depends on it.
            print(f"❌ The restore failed partway through: {e.cause}")
            locations = ", ".join(
                _safe_name(str(p)) for p in (e.backup, e.store_backup) if p is not None
            )
            print(
                "   Putting your previous state back did NOT fully succeed. Some of it "
                f"exists only in {locations} now:"
            )
            for item in e.failed[:10]:
                print(f"     {_safe_name(item)}")
            if len(e.failed) > 10:
                print(f"     (+{len(e.failed) - 10} more)")
            print(
                "   Recover those by hand BEFORE re-running, or the next restore's "
                "rollback set will be taken from this half-reverted state."
            )
            _audit(
                "state_restore_rejected",
                f"reason=rollback_incomplete from={snap_path.name}: {e.cause}",
            )
            return 1
        except (OSError, DatabaseCopyFailed) as e:
            # A full disk, a read-only filesystem, or a file another process holds open
            # fails MID-mutation, which is a different answer from the refusals above:
            # `_do_replace` has already put the whole saved set back and re-raised. So the
            # home is on its pre-restore generation and the operator needs to be told that
            # much -- a traceback says a restore blew up without saying what state they are
            # now in, which is the one thing they need to know before retrying.
            print(f"❌ The restore failed partway through: {e}")
            print("   Your previous state was put back; nothing from the bundle remains.")
            _audit(
                "state_restore_rejected",
                f"reason=io_failure from={snap_path.name}: {e}",
            )
            return 1

    # Integrity check
    if _want(components, "memory") and (mc / "memory.db").is_file():
        try:
            # `closing`, not a bare `with sqlite3.connect(...)`: the connection's own
            # context manager ends the TRANSACTION and leaves the handle open. Windows
            # refuses to move or replace a file that still has one, so a leak here makes
            # the NEXT restore in the same process fail on the database this one just
            # installed -- and leaves the restored file held open either way.
            with closing(sqlite3.connect(str(mc / "memory.db"))) as conn:
                result = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        except Exception as e:
            result = str(e)
        if result == "ok":
            print("🔍 memory.db integrity: OK")
        else:
            print(f"⚠️  memory.db integrity check failed: {result}")
            _audit("state_restore_rejected", f"reason=integrity_check_failed from={snap_path.name}")
            return 1
        if not (mc / "memory_index.db").is_file():
            print(
                "⚠️  memory_index.db is missing — full-text search may not "
                "work until the FTS index is rebuilt."
            )

    comp_str = ",".join(components) if components else "all"
    _audit("state_restored", f"mode={mode} components={comp_str} from={snap_path.name}")

    print("\n⚠️  Restart kirocrew gateway to pick up changes: kirocrew restart")
    return 0
