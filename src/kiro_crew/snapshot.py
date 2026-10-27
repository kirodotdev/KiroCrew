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
* :mod:`kiro_crew.snapshot_build` -- building a bundle: the size estimate and the staging
  pass that writes ``MANIFEST.json`` and publishes the tarball
* :mod:`kiro_crew.snapshot_merge_driver` -- the ``--mode merge`` driver and the
  notification copy
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil  # noqa: F401 - facade re-exports (tests patch snapshot.shutil)
import socket
import sys
import tarfile
import tempfile
from contextlib import ExitStack, closing  # noqa: F401 - facade re-exports
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
    UnknownMemoryStore,
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
    MAX_ARCHIVE_GIB_CEILING,
    SKIP_DB_UNPINNED_SOURCE,
    DatabaseCopyFailed,
    ManifestUnreadable,
    _ArchiveTooLarge,
    _bound_gib,
    _bundle_carries_named_stores,
    _chain_is_link_free,
    _copy_database_consistently,
    _copytree_safe,
    _data_filter,
    _dir_flags_nofollow,
    _escape_one,
    _hold_reader_lock,
    _immutable_read_may_stand_in,
    _manifest_components,
    _print_manifest,
    _refuse_if_changed_since,
    _refuse_oversized_archive,
    _refuse_oversized_tree,
    _refuse_unsound_required_capture,
    _rejection_recording_filter,
    _report_skip,
    _restage_databases,
    _safe_name,
    _staging_ignore,
    _staging_is_pinned,
    _terminal_safe,
    archive_byte_bound,
)
from kiro_crew.snapshot_build import (  # noqa: F401 - facade re-exports
    _build_snapshot,
    _estimate_selected_bytes,
    _fsize,
)
from kiro_crew.snapshot_components import (  # noqa: F401 - facade re-exports
    _CONFIG_SETTINGS_DOCUMENTS,
    _CORE_FILE_COMPONENTS,
    _DERIVED_INDEXES,
    _FLAT_DOCUMENT_VALIDATORS,
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
    _core_file_not_installed,
    _cron_rewrite_in_place,
    _install_core_file_if_absent,
    _install_notifications,
    _merge_crons,
    _merge_memory,
    _merge_named_stores,
    _merge_notifications,
    _notification_key,
    _open_notification_file,
    _read_cron_store_once,
    _report_unmerged_databases,
    _serialise_with_notification_writes,
    _usable_cron_shape,
    _validate_identifier,
    _write_merged_crons,
)
from kiro_crew.snapshot_merge_driver import (  # noqa: F401 - facade re-exports
    _copy_notifications,
    _do_merge,
)
from kiro_crew.snapshot_restore import (  # noqa: F401 - facade re-exports
    _OWNER_ONLY_SETTINGS_FILES,
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
    _refuse_dropped_entries,
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


def _list_components() -> None:
    print("Available components:")
    for k, v in COMPONENT_HELP.items():
        print(f"  {k:16s} {v}")
    print("\nCombine with commas: --components memory,crons,skills")


def _warn_if_transcripts_left_behind(mc: Path, components: list[str] | None) -> None:
    """Say so when config is restored: no component carries the chats themselves.

    The dashboard lists chats from the ``sessions/*.jsonl`` files, not from
    ``session_map.json`` (that is the kiro-cli resume map), and no component
    carries ``sessions/``.
    """
    if not _want(components, "config"):
        return
    print(
        "⚠️  WARNING: chat history is not included in this restore. No component carries "
        f"sessions/ (the dashboard's chats), so {mc / 'sessions'} is left as it is and chats "
        "from the source machine will not appear."
    )


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


def _report_unresolved_payload(selected: list[str], *, uploading: bool = True) -> None:
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
    if not uploading:
        # A local bundle: no off-host copy follows, so there is no "below" to point to.
        print(
            "   `config` carries credentials in plaintext at rest, and so does this "
            "bundle. Treat it as secret, and narrow it with --components if you do not "
            "need all of it."
        )
        return
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


#: Exit status of `kirocrew snapshot --strict` when the bundle was written but omits
#: entries it was asked to carry. Distinct from 1 (no usable bundle) so a caller can
#: tell "written but incomplete" apart from "failed". Without `--strict` such a run
#: still exits 0, because existing callers treat any non-zero exit as a failed backup.
EXIT_INCOMPLETE = 3

_MAX_ARCHIVE_GIB_HELP = (
    "Set the bound on a bundle's uncompressed content in GiB (default 4, at most "
    f"{MAX_ARCHIVE_GIB_CEILING}) for this run only. A bundle written past the default "
    "restores with a --max-archive-gib at least as large on restore."
)


def _archive_bound_from_args(args: argparse.Namespace) -> int | None:
    """The byte bound this run enforces, or ``None`` after printing why the flag is refused.

    One validator for both commands, so `snapshot` can never write a bound `restore`
    would not accept. An absent flag is the default bound, never an error.
    """
    gib = getattr(args, "max_archive_gib", None)
    if gib is not None and not 1 <= gib <= MAX_ARCHIVE_GIB_CEILING:
        print(f"❌ --max-archive-gib must be between 1 and {MAX_ARCHIVE_GIB_CEILING}, got: {gib}")
        return None
    return archive_byte_bound(gib)


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
        p.add_argument(
            "--strict",
            action="store_true",
            help=(
                "Exit 3 instead of 0 when the bundle was written but omits entries it "
                "was asked to carry (see MANIFEST.json 'skipped'). The bundle is kept."
            ),
        )
        p.add_argument("--to", default=None, help=argparse.SUPPRESS)
        p.add_argument(
            "--max-archive-gib",
            type=int,
            default=None,
            dest="max_archive_gib",
            help=_MAX_ARCHIVE_GIB_HELP,
        )
        parsed = p.parse_args(argv)
    args = parsed
    allow_unpinned = bool(getattr(args, "allow_unpinned", False))

    if args.keep <= 0:
        print(f"❌ --keep value must be a positive integer, got: {args.keep}")
        return 1
    archive_bound = _archive_bound_from_args(args)
    if archive_bound is None:
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
            max_archive_bytes=archive_bound,
        )
    except _ArchiveTooLarge as e:
        # Refused while staging, before any tarball existed: nothing is published under
        # the name retention and restore look for, and nothing is pruned.
        _audit("snapshot_rejected", f"reason=archive_too_large detail={e}")
        print(f"❌ {e}.")
        if e.kind == "entries":
            # The entry-count bound is fixed, so the override would refuse the same way.
            print(
                "   No bundle was written and nothing was pruned. Leave a component out "
                "with --components."
            )
        else:
            print(
                "   No bundle was written and nothing was pruned. Leave a component out with "
                "--components, or pass --max-archive-gib N here and at least N to "
                "`kirocrew restore`."
            )
        return 1
    except pinned_fs.PinnedPathRefusal as exc:
        # A refusal is a decision this command made on purpose. A traceback would
        # read like a crash and bury the sentence saying what to do about it.
        #
        # It is also a PERMISSION decision, so it belongs in the SEL log next to
        # `state_restore_rejected`. Without the audit, the one outcome a reviewer would
        # most want a record of -- staging declined on an unsupported platform -- would
        # leave no trace.
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
    except UnknownMemoryStore as e:
        # A memory store refused the read, most often through its locks: one is redirected
        # or not a private regular file, or the stores changed while they were read without
        # the namespace lock. The detail names the cause. Raised before the archive was
        # published, so it is a refusal and reads as one.
        _audit("snapshot_rejected", f"reason=memory_store_refused detail={e}")
        print(f"❌ {e}")
        print("   No bundle was written.")
        return 1

    sz = outfile.stat().st_size
    human = f"{sz // 1024}K" if sz < 1024 * 1024 else f"{sz / 1024 / 1024:.1f}M"

    # The bound belongs at CREATION, not only on the paths that move a bundle around. A
    # bundle past it cannot be restored by this tool, so reporting success would promise a
    # backup that does not exist -- and the prune below would then delete older bundles that
    # DO restore in favour of one that never will. Checked before both. The staged tree was
    # already held to the same bound, so this read-back is the backstop for an archive
    # whose headers disagree with what was staged.
    try:
        with tarfile.open(outfile) as probe:
            _refuse_oversized_archive(probe, max_bytes=archive_bound)
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
    # The bundle on local disk is not redacted either, so it gets the same notice as
    # the copy that leaves the host: which components carry credential material.
    _report_unresolved_payload(selected, uploading=False)

    _audit("snapshot_created", f"{outfile} ({human})")

    # Prune. --keep is a promise about local disk, so retention runs after every
    # successful bundle unless the omission guard below holds it: a daily backup must
    # not turn into an unbounded pile of bundles -- the disk fills, and then the
    # snapshot that would have worked cannot be written either.
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
    # Opt-in only. The same predicate that held the prune decides the exit, so a caller
    # never has to re-classify `skipped` reasons itself.
    if omitted and getattr(args, "strict", False):
        print(f"⚠️  --strict: exiting {EXIT_INCOMPLETE} because this bundle is incomplete.")
        return EXIT_INCOMPLETE
    return 0


# ── Restore ───────────────────────────────────────────────────────────────────


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


def _hold_back_executables(
    snap: Path, mc: Path, components: list[str] | None, mode: str
) -> dict[str, list[str]]:
    """Switch off, in the staged bundle, every cron and hook that would run a command.

    Uses the dashboard import's own helpers, so both ways in apply one rule: a
    command or script cron is restored paused (a command failing the ``cron_add``
    vet is dropped), and a script hook is restored disabled. Message-only crons and
    skill-only hooks run nothing and are left as they are. Returns the names, for
    :func:`_report_held_back`, keyed ``dropped`` / ``paused`` / ``hooks``, plus
    ``kept_crons``: the names a merge's destination already has.
    """
    # Imported here: the portability family imports this module at load time.
    from kiro_crew import portability

    _disable_imported_script_hooks = portability._disable_imported_script_hooks
    _sanitize_imported_crons = portability._sanitize_imported_crons

    held: dict[str, list[str]] = {"dropped": [], "paused": [], "hooks": [], "kept_crons": []}
    if _want(components, "crons"):
        # The restore judges the store's shape itself (merge skips an unusable one,
        # replace validates it), so only the vet and the pause are applied here.
        held["dropped"], held["paused"] = _sanitize_imported_crons(
            snap / "crons.json", drop_malformed=False
        )
        if mode == "merge":
            # A merge skips a bundle job whose name the home already has, so those
            # names are not the bundle's and must not be reported as restored.
            held["kept_crons"] = sorted(_cron_names(mc / "crons.json"))
    if _want(components, "config"):
        # Merge keeps an existing hooks.json, so the bundle's hooks only land where
        # there is none. Still switched off either way; only named when installed.
        installs_hooks = mode == "replace" or not os.path.lexists(mc / "hooks.json")
        hooks = _disable_imported_script_hooks(snap / "hooks.json")
        if installs_hooks:
            held["hooks"] = hooks
    return held


def _cron_names(path: Path) -> set[str]:
    """The job names in a cron store, or none when it cannot be read as one.

    Read through the merge's own one-open reader: non-blocking, so a FIFO at the
    name cannot hang the restore, and anything but one regular file is refused.
    """
    try:
        text, _seen, _fd = _read_cron_store_once(path, what="cron store")
        data = json.loads(text)
    except (OSError, ValueError, RecursionError):
        return set()
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return set()
    return {j["name"] for j in jobs if isinstance(j, dict) and isinstance(j.get("name"), str)}


def _report_held_back(held: dict[str, list[str]], mc: Path) -> None:
    """Name what the restore switched off or dropped, and how to turn it back on.

    A paused job is named only when it is in the live store after the restore and
    was not there before: a merge skips a same-named job and can refuse the whole
    cron file, and neither installs anything to switch on.
    """
    if held["paused"]:
        installed = _cron_names(mc / "crons.json") - set(held["kept_crons"])
        held["paused"] = [name for name in held["paused"] if name in installed]
    if held["dropped"]:
        print(
            "⚠️  Not restored: these cron jobs have a command that fails the check "
            "`cron_add` applies:"
        )
        for name in held["dropped"]:
            print(f"     {_safe_name(_terminal_safe(name))}")
    if held["paused"]:
        print("⏸  Restored PAUSED: these cron jobs run a command or script:")
        for name in held["paused"]:
            print(f"     {_safe_name(_terminal_safe(name))}")
        print("   Check each one, then resume it on the dashboard's Schedules page.")
    if held["hooks"]:
        print("⏸  Restored DISABLED: these hooks run a shell command:")
        for name in held["hooks"]:
            print(f"     {_safe_name(_terminal_safe(name))}")
        print("   Check each one, then enable it on the dashboard's Hooks page.")


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
        p.add_argument(
            "--max-archive-gib",
            type=int,
            default=None,
            dest="max_archive_gib",
            help=_MAX_ARCHIVE_GIB_HELP,
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

    archive_bound = _archive_bound_from_args(args)
    if archive_bound is None:
        return 1
    raised_bound = getattr(args, "max_archive_gib", None) is not None

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
                declared_bytes = _refuse_oversized_archive(tar, max_bytes=archive_bound)
                if raised_bound:
                    # Past the default bound the operator vouched for the size, not for
                    # the disk. Holding a raised bound to the free space of the extraction
                    # directory keeps the override from turning a hostile or simply wrong
                    # archive into a full disk; the default bound needs no such check.
                    free = shutil.disk_usage(work).free
                    if declared_bytes > free:
                        raise _ArchiveTooLarge(
                            f"This archive declares {declared_bytes / (1024 ** 3):.2f} GiB of "
                            "uncompressed content, more than the "
                            f"{free / (1024 ** 3):.2f} GiB free where it would be extracted"
                        )
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
        _warn_if_transcripts_left_behind(mc, components)

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
        # A bundle may come from someone else, so anything in it that runs a shell
        # command on its own is installed switched off -- the same rule the dashboard
        # import applies. Done on the staged copy, after the source checks above have
        # judged the files as the archive carried them, and before either mode reads it.
        try:
            held_back = _hold_back_executables(snap, mc, components, mode)
        except OSError as exc:
            # Only the staged copy was touched: live state is as it was.
            _audit("state_restore_rejected", f"reason=staging_rewrite_failed detail={exc}")
            print(f"❌ Could not prepare the bundle's crons and hooks: {exc}")
            print("   Nothing was restored; your current state is unchanged.")
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

    _report_held_back(held_back, mc)
    print("\n⚠️  Restart kirocrew gateway to pick up changes: kirocrew restart")
    return 0
