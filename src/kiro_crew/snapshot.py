"""KiroCrew snapshot and restore — portable state management."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import socket
import stat as _stat
import tarfile
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.jsonl_util import (
    RECORD_CAP,
    UndecodableRecord,
    UnreadableRecord,
    strict_raw_records,
)

if TYPE_CHECKING:
    from kiro_crew import snapshot_redact

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

try:
    from kiro_crew.config.loader import DASHBOARD_PORT as _DASHBOARD_PORT
except Exception:  # pragma: no cover - optional during early/standalone import
    _DASHBOARD_PORT = int(os.environ.get("KIROCREW_PORT", 5476))


# Files that must always have 0o600 permissions in snapshots and on restore.
SECURITY_SENSITIVE_FILES: frozenset = frozenset({"sel_hmac.key", "telemetry_salt"})

# Must match ``handlers_system._get_telemetry_salt`` (``secrets.token_bytes(32)``).
# ``_copy_locked`` loads telemetry_salt into memory, so a planted giant in an
# untrusted snapshot would OOM restore after earlier merge steps.
_TELEMETRY_SALT_BYTES = 32

# Files that must NEVER ride a snapshot: sel_hmac.key is regenerated on restore
# so audit-log HMACs stay bound to the host that wrote them.
#
# This set is matched by BASENAME inside `_data_filter`, which runs over the
# ENTIRE tar — including the staged workspace/, plan_memory/ and skills/ trees.
# So any name added here also silently drops a USER file that happens to share
# it. Keep the set minimal for that reason.
#
# The beacon's per-install identity (beacon_install_id / beacon_last_sent) is
# deliberately NOT here: snapshot staging copies an explicit per-component file
# list (CORE_FILES) plus those three directories, and no component lists a beacon
# file, so a root beacon file is never staged in the first place. The
# id-cloning hazard is closed by that non-selection, not by a basename filter.
NEVER_SNAPSHOT_FILES: frozenset = frozenset({"sel_hmac.key"})


def _redactor() -> Any:
    """The outbound redaction pass, imported on first use.

    `snapshot` is on the gateway's boot path, so importing the redaction code here would
    put it there too -- and a gateway never redacts anything. Resolved when a command that
    actually prepares an outbound copy asks for it, so `kirocrew gateway` reaches
    readiness without loading it. A ratchet compares the boot module set against the base
    branch's.
    """
    return importlib.import_module("kiro_crew.snapshot_redact")


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _escape_one(ch: str) -> str:
    """Render one stripped character in a form that cannot be re-interpreted.

    Two widths, because a single `\\xNN` spelling would be a lie for a code point above
    0xFF: `\\x202e` reads as `\\x20` followed by a literal `2e`, which is exactly the kind
    of ambiguous output this function exists to prevent.
    """
    code = ord(ch)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def _safe_name(value: object, default: str = "unknown", limit: int = 300) -> str:
    """Render a name that came out of an ARCHIVE printable.

    Tar member names, manifest keys and archive root directories are all chosen by
    whoever wrote the bundle. Printing one raw means the terminal INTERPRETS whatever
    escape sequences it holds: the cursor moves, lines get overwritten, and a hostile
    archive can dress itself up as a different, expected one -- right above the prompt
    where the operator decides whether to restore it. Two of these sites print while
    REJECTING a hostile entry, so the raw name there is precisely the attacker's payload.

    The escaping itself used to live beside the off-host destination code, because its
    first caller printed S3 object keys. That destination is now the AWS Control app's,
    and what is left to escape is a name out of an untrusted archive -- so the helper
    lives with its remaining caller and its reason is stated in those terms. The length
    is capped so one very long name cannot flood the view.
    """
    cleaned = _CONTROL_CHARS.sub(lambda m: _escape_one(m.group()), str(value if value else default))
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…(truncated)"
    return cleaned


def _rejection_recording_filter(
    rejected: list[str],
) -> "Callable[..., tarfile.TarInfo | None]":
    """`_data_filter`, plus a record of every entry it DROPPED for a structural reason.

    Extraction prints a warning and drops a rejected entry, then extraction continues -- so a
    bundle can arrive at the restore missing part of its payload while the manifest still
    declares it. In replace mode that is destructive rather than merely incomplete: the memory
    trees are cleared unconditionally (a tree the archive lacks must not be kept) and nothing
    replaces the one that was dropped.

    This is the ONLY layer where the two cases are distinguishable. Measured: at the mutation
    phase a rejected link and an archive that never carried the tree are byte for byte the same
    state -- the staged tree is simply absent -- so the prescribed check there ("clear only when
    the source is a directory") cannot tell them apart, and it would revert the unconditional
    clear that a documented defect required. Extraction, by contrast, knows.

    A `NEVER_SNAPSHOT_FILES` drop is NOT recorded: those are deliberate and expected. Nor is a
    rejection anywhere OUTSIDE a tree that replace clears -- and that limit is the point.
    `test_symlink_filtered_out` states the contract for those: a hostile entry injected into an
    otherwise sound bundle is dropped and the restore SUCCEEDS (`assert ret == 0`). Refusing on
    any rejection at all was the prescribed shape and breaks three of those tests. What makes the
    cleared trees different is that dropping an entry there converts into DELETION of the
    operator's own tree, rather than merely into an absence.
    """
    cleared_trees = {"workspace", "plan_memory", "skills"}

    def _f(info: tarfile.TarInfo, dest: str = "") -> tarfile.TarInfo | None:
        kept = _data_filter(info, dest)
        if kept is None and PurePosixPath(info.name).name not in NEVER_SNAPSHOT_FILES:
            # Entries are `<bundle-root>/<tree>/...`; the tree is what decides.
            parts = PurePosixPath(info.name).parts
            if len(parts) > 1 and parts[1] in cleared_trees:
                rejected.append(_safe_name(info.name))
        return kept

    return _f


def _data_filter(info: tarfile.TarInfo, _dest: str = "") -> tarfile.TarInfo | None:
    """Equivalent to tarfile ``"data"`` filter (Python 3.12+), with 3.10 fallback.

    Also rejects path traversal, symlinks, and hardlinks to eliminate TOCTOU
    race between pre-scan and extraction.
    Excludes sel_hmac.key (must be regenerated on restore, not shipped).
    Security-sensitive files get 0o600 permissions.
    """
    # Reject path traversal. POSIX checks apply everywhere; the Windows-syntax
    # checks (backslash separators, drive letters — incl. the drive-RELATIVE
    # `C:foo` form is_absolute() misses, which resolves against the drive CWD
    # at extraction) apply ONLY when extracting on Windows, where tarfile
    # honors '\' as a native separator. They must NOT run on POSIX: ':' and
    # '\' are legal characters in Linux/macOS filenames, so a workspace file
    # named `a:1` or `notes..\old` would be silently dropped from a
    # Linux-to-Linux restore.
    name = info.name
    traversal = (
        name.startswith("/")
        or ".." in PurePosixPath(name).parts
        or PurePosixPath(name).is_absolute()
    )
    if not traversal and platform_compat.IS_WINDOWS:
        traversal = (
            name.startswith("\\")
            or ".." in PureWindowsPath(name).parts
            or PureWindowsPath(name).is_absolute()
            or bool(PureWindowsPath(name).drive)
        )
    if traversal:
        print(f"⚠️  Rejecting path traversal entry: {_safe_name(info.name)}")
        return None
    # Reject symlinks and hardlinks
    if info.issym() or info.islnk():
        print(f"⚠️  Rejecting symlink/hardlink entry: {_safe_name(info.name)}")
        return None
    # Never ship these — each must be regenerated on the restoring host.
    basename = PurePosixPath(info.name).name
    if basename in NEVER_SNAPSHOT_FILES:
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    # Security-sensitive files get restricted permissions
    if not info.isdir() and basename in SECURITY_SENSITIVE_FILES:
        info.mode = 0o600
    else:
        info.mode = 0o755 if info.isdir() else 0o644
    return info


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


class Purpose(str, Enum):
    """Why a bundle exists. Decides which components may ride in it.

    A bundle's purpose is not cosmetic: ``BACKUP`` restores onto a replacement host
    the operator already controls, so it wants the credentials that make recovery
    turnkey. ``SHARE`` leaves the operator's control, so a component that carries
    credential material must not ride in one. Recording the purpose in the manifest
    is what lets a reader of a bundle know which of the two they are holding.
    """

    BACKUP = "backup"
    SHARE = "share"


class SecretPolicy(str, Enum):
    """A component's declaration about the credential material it carries.

    Every component must declare one. There is deliberately no default: a component
    added without a declaration is refused at staging (see :func:`resolve_components`)
    rather than inheriting whichever value happens to be permissive.

    ``UNRESOLVED`` means nobody has established that the component is safe to hand to
    another person. It rides a ``BACKUP`` bundle unchanged and is refused outright in
    a ``SHARE`` bundle.

    ``SHARE_SAFE`` means someone has, and **no component claims it today**. That is
    not an oversight. Whether a component is safe to share is a question about
    CONTENT, not structure: a workspace file, a skill, a cron's ``env`` map, a
    notification body or a pasted lesson can each contain a token, and staging cannot
    tell. Two components were flipped from a guessed-safe value to ``UNRESOLVED``
    during review of this change, one at a time, before the pattern was obvious. The
    value is kept so the seam has both sides and the gate stays exercised; the first
    genuinely certified component will arrive with the redaction work that earns it.
    """

    SHARE_SAFE = "share-safe"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ComponentSpec:
    """What one component stages, and its credential declaration.

    ``files`` are data-home-relative files copied individually; ``trees`` are
    data-home-relative directories copied wholesale. A ``.db`` file in ``files`` is
    copied through the SQLite backup API rather than the filesystem, so a live
    gateway holding the database open still yields a consistent copy.
    """

    policy: SecretPolicy
    help: str
    files: tuple[str, ...] = ()
    trees: tuple[str, ...] = ()


# The single source of truth for what a bundle can contain. Both the staging path
# and the restore path read this, so a component cannot be stageable but
# unrestorable (or the reverse) without the mismatch being visible here.
COMPONENTS: dict[str, ComponentSpec] = {
    # Self-contained on purpose: lessons and semantic/episodic recall live in the two
    # databases, but the markdown half of memory lives under workspace/. Naming those
    # trees here means restoring memory does not require restoring the whole
    # workspace, which on a real install is two orders of magnitude larger.
    "memory": ComponentSpec(
        # UNRESOLVED like every other component: a lesson or a note can contain a
        # token somebody pasted, and staging cannot tell. Memory is NOT redacted in a
        # backup -- that is the whole point of backing it up -- this declaration only
        # governs whether it may ride a bundle that leaves the operator's control.
        policy=SecretPolicy.UNRESOLVED,
        help=(
            "memory.db, memory_index.db (semantic, episodic, lessons), "
            "workspace/memory/ (preferences, projects, history), workspace/knowledge/ "
            "(files; the knowledge database is replaced, not row-merged)"
        ),
        files=("memory.db", "memory_index.db"),
        trees=("workspace/memory", "workspace/knowledge"),
    ),
    "crons": ComponentSpec(
        # `CronJob.env` is a persisted dict of per-job environment variables
        # (cron.py), so a job passing an API token carries it in crons.json.
        policy=SecretPolicy.UNRESOLVED,
        help="crons.json (scheduled jobs)",
        files=("crons.json",),
    ),
    "config": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="config.json, session_map.json, hooks.json, project_dir, workspace_dir",
        files=("config.json", "session_map.json", "hooks.json", "project_dir", "workspace_dir"),
    ),
    "skills": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="skills/ directory",
        trees=("skills",),
    ),
    "workspace": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="workspace/, plan_memory/ directories",
        trees=("workspace", "plan_memory"),
    ),
    "notifications": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="notifications.jsonl (notification history)",
        files=("notifications.jsonl",),
    ),
    "security": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="telemetry_salt (sel_hmac.key excluded — regenerated on restore)",
        files=("telemetry_salt",),
    ),
}


class ComponentRefused(Exception):
    """A requested component cannot ride a bundle of the requested purpose."""


def resolve_components(requested: list[str] | None, purpose: Purpose) -> list[str]:
    """Return the component names to stage, or raise :class:`ComponentRefused`.

    ``None`` means every component. The two refusals are the seam's whole point:
    an unknown name never silently stages nothing, and an ``UNRESOLVED`` component
    never rides a ``SHARE`` bundle just because nobody wrote the policy down.

    Duplicates are collapsed, ORDER PRESERVED. ``--components config,config`` used to reach
    the staging pass twice for one component, and the second pass hit the exclusive create
    the pinned primitives make -- an uncaught ``FileExistsError`` traceback rather than a
    snapshot. A repeated name is a typo, not a request to stage anything twice, so the
    honest reading is to collapse it rather than to refuse the run.
    """
    names = list(COMPONENTS) if requested is None else list(dict.fromkeys(requested))
    unknown = [c for c in names if c not in COMPONENTS]
    if unknown:
        raise ComponentRefused(
            f"unknown component(s): {', '.join(sorted(unknown))} "
            f"(known: {', '.join(sorted(COMPONENTS))})"
        )
    if purpose is Purpose.SHARE:
        blocked = [c for c in names if COMPONENTS[c].policy is SecretPolicy.UNRESOLVED]
        if blocked:
            raise ComponentRefused(
                f"component(s) {', '.join(sorted(blocked))} have no share-safe policy, "
                f"so they cannot ride a '{Purpose.SHARE.value}' bundle. Whether a "
                f"component is safe to hand to someone else is a question about its "
                f"CONTENT — a workspace file, a skill, a cron's env map or a pasted "
                f"lesson can each hold a token — and no component is certified yet. "
                f"Use --purpose {Purpose.BACKUP.value} to back up onto a host you "
                f"control."
            )
    return names


# Derived views, kept because callers and tests read them as the component tables.
CORE_FILES: dict[str, tuple[str, ...]] = {
    name: spec.files for name, spec in COMPONENTS.items() if spec.files
}

# Every core-file name, flattened across components. DERIVED, never hand-listed: recovery
# uses it to tell "this run would have created a regular FILE here" from "something else's
# directory is standing at that name", and a hand-kept copy of the list would drift from the
# component specs exactly when a new component is added -- which is when the distinction
# matters most.
CORE_FILES_FLAT: frozenset[str] = frozenset(f for files in CORE_FILES.values() for f in files)

# Core files that are DERIVED: regenerable from the payload they index, and dropped from an
# off-host bundle by the redaction pass for exactly that reason. Replace has to answer for
# the consequence -- see `_drop_derived_indexes_absent_from_bundle`.
#
# Duplicated from `snapshot_redact._DERIVED_INDEXES` rather than imported: that module is
# loaded LAZILY here (`_redact_module()`), and an eager import for one frozenset would pull
# it into the boot path that `test_perf_boot_path.py` guards. A test asserts the two sets
# agree, so a future divergence fails loudly instead of silently restoring a stale index.
_DERIVED_INDEXES: frozenset[str] = frozenset({"memory_index.db"})


# Component files whose consumers read a JSON OBJECT and degrade silently when they do
# not get one. `crons.json` is the sharpest case: its loader wraps `json.loads` in a
# `try` and falls back to "no jobs", and even a well-formed JSON *array* takes the
# `isinstance(data, dict) else []` branch — so a corrupt file discards every scheduled
# job while the restore reports success.
#
# Listed rather than derived, because "ends in .json" is not the property that matters:
# what matters is that a consumer treats an unreadable file as empty instead of as an
# error. A component file added here is validated before it can be installed.
COMPONENT_JSON_OBJECTS: frozenset[str] = frozenset(
    {
        "crons.json",
        "config.json",
        "session_map.json",
        "hooks.json",
    }
)

# Keys inside those files whose value must be a LIST OF OBJECTS, because a reader iterates
# the list and reads fields off each entry. An object at the top level is necessary and not
# sufficient: `{"jobs": ["x"]}` is a valid object whose consumer reaches `.get` on a `str`
# and raises halfway through a merge, with live state already partly changed.
_JSON_OBJECT_LISTS: dict[str, tuple[str, ...]] = {
    "crons.json": ("jobs",),
}

# The tree counterpart of CORE_FILES. Derived from the same specs so a component that
# gains a tree is covered by everything keyed on this without a second edit.
COMPONENT_TREES: dict[str, tuple[str, ...]] = {
    name: spec.trees for name, spec in COMPONENTS.items() if spec.trees
}

# Databases this product owns that live INSIDE a component tree rather than at the top
# level. Paths are relative to a bundle root, POSIX-separated.
#
# They cannot be derived from `ComponentSpec.files`, which names only top-level files, so
# they are listed. The list is what separates "our database, broken bundle" from "a `.db`
# the operator happens to keep in their own folder": everything here is validated as
# strictly as `memory.db`, and everything else under a tree is only checked when it opens
# as a database at all. A product database added under a tree and left off this list is
# validated leniently, which is the failure this comment exists to prevent.
PRODUCT_TREE_DATABASES: frozenset[str] = frozenset(
    {
        "workspace/knowledge/knowledge.db",
    }
)

COMPONENT_HELP = {name: spec.help for name, spec in COMPONENTS.items()}

VALID_COMPONENTS: tuple[str, ...] = tuple(COMPONENTS)


def _mc_dir() -> Path:
    # Use the shared resolver so snapshot/restore honor the documented
    # KIROCREW_HOME override (and the same ~/.kiro/crew default) as every other
    # module — not an undocumented KIROCREW_DIR, which would make snapshots
    # silently target the real home even when state was relocated.
    from kiro_crew.config.loader import config_dir

    return config_dir()


# SQLite sidecars are excluded from every staged tree. They describe the SOURCE
# database's in-flight transaction state; shipping them next to a consistent backup
# copy would invite the restoring host to replay a journal that does not match it.
#
# Not redundant with _restage_databases, though it looks that way: re-opening a
# staged database makes SQLite discard the copied sidecars as a side effect, so for a
# real database either mechanism alone appears to work. This glob is what covers the
# case _restage_databases SKIPS — a file named .db that SQLite cannot open, whose
# stray sidecars would otherwise ride.
_DB_SIDECAR_GLOBS = ("*.db-wal", "*.db-shm", "*.db-journal", "*.sqlite3-wal", "*.sqlite3-shm")

# Suffixes treated as SQLite databases when found inside a staged tree.
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


class DatabaseCopyFailed(Exception):
    """A readable database could not be copied consistently.

    Carries the source path so the command boundary can name the file. Raised rather
    than absorbed because the staged copy at that point is a raw byte copy without its
    WAL sidecars — shipping it would put a torn database in a bundle that reports
    success — and typed rather than bare so the failure exits with a message instead of
    a traceback.
    """

    def __init__(self, path: Path, cause: Exception) -> None:
        super().__init__(f"{path}: {cause}")
        self.path = path


def _chain_is_link_free(root: Path, rel_parts: tuple[str, ...]) -> bool:
    """Is every component of *rel_parts* under *root* a real directory, not a link?

    Walked with descriptors: each directory is opened relative to the previous one with
    ``O_NOFOLLOW``, so a component that is a link -- or one swapped for a link while this
    pass runs -- fails its own open instead of redirecting the walk. The final component is
    the file itself and is checked as a regular file through its pinned parent.

    This exists because verifying a path and then RE-WALKING it by name are two different
    resolutions of the same string: the second can land somewhere the first never inspected.
    ``resolve()`` and a late ``realpath()`` are both that second walk.

    Requires descriptor pinning, and SAYS SO rather than pretending: where the platform
    cannot open relative to a directory descriptor (``os.open`` absent from
    ``os.supports_dir_fd``, or no ``O_NOFOLLOW`` -- which is Windows), this returns True and
    the caller proceeds on the by-name screening the loop already did, the file being a
    regular file and not a link or reparse point. That is weaker, and it is the same
    degradation the rest of this module applies through ``_staging_is_pinned``. Returning
    False instead would refuse every database on that platform, turning a hardening into an
    outage.

    The first version omitted that gate and passed ``dir_fd`` unconditionally, which is not
    merely weaker on Windows -- ``os.open`` RAISES ``NotImplementedError`` there, so the pass
    crashed. ``supports_pinned_walk`` exists for exactly this.
    """
    if not pinned_fs.supports_pinned_walk():
        return True
    try:
        fd = pinned_fs.open_dir_pinned(root, what="database source root")
    except OSError:
        # Gone, or otherwise unopenable: the same answer the intermediate components below
        # already give, and for the same reason -- a root this pass cannot open is a file it
        # cannot verify as reachable without following a link.
        #
        # `open_dir_pinned` translates only `ELOOP`/`ENOTDIR` into `PinnedPathRefusal` and
        # re-raises every other `OSError`, and this call sat OUTSIDE the try below. So a root
        # lost to a concurrent rename or removal left `ENOENT` escaping as
        # `FileNotFoundError` from a call site under `_build_snapshot`, whose enclosing try
        # handles `PinnedPathRefusal`, `UnsafeComponentRoot` and `DatabaseCopyFailed` -- the
        # `OSError` arm belongs to a later, separate try. The command exited on a traceback
        # where the declared path owes a named refusal and the tree path owes a recorded
        # skip. Returning False routes both through the `require_database` asymmetry the
        # caller already implements. Review's finding.
        #
        # `PinnedPathRefusal` is deliberately NOT caught here: `snapshot_main` handles it and
        # audits it as `unpinnable_staging`. That is a decision the operator should see, not
        # a source that went missing.
        return False
    try:
        for part in rel_parts[:-1]:
            try:
                nxt = os.open(part, _dir_flags_nofollow(), dir_fd=fd)
            except OSError:
                return False  # a link, or gone: either way this file is not reachable safely
            os.close(fd)
            fd = nxt
        return pinned_fs.is_regular_at(fd, rel_parts[-1])
    except pinned_fs.PinnedPathRefusal:
        return False
    finally:
        os.close(fd)


def _dir_flags_nofollow() -> int:
    """``O_RDONLY|O_DIRECTORY|O_NOFOLLOW``, with the flags that only exist on some platforms
    added when present."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return flags | getattr(os, "O_CLOEXEC", 0)


# Outcomes of `_copy_database_consistently`. Three, not a bool, because the two
# non-success cases need OPPOSITE handling from the caller and collapsing them is how a
# bundle ends up carrying a database nobody copied consistently while reporting success:
#
#   COPIED         the backup API produced a consistent copy at the destination.
#   NOT_A_DATABASE the file is positively NOT SQLite, so the caller should stage its bytes
#                  -- a non-database named `.db` is still the operator's file.
#   UNSAFE_SOURCE  the source could not be verified as reachable without traversing a
#                  link, so nothing was read from it. The caller must NOT substitute a
#                  byte copy: that is the read this refusal exists to prevent.
#
# Anything else raises `DatabaseCopyFailed`. A database that IS readable but could not be
# copied is never degraded to a byte copy, because this module excludes `-wal`/`-shm` and
# such a copy would be a torn database shipped as a whole one.
DB_COPIED = "copied"
DB_NOT_A_DATABASE = "not_a_database"
DB_UNSAFE_SOURCE = "unsafe_source"

# Recorded in MANIFEST.json when a database was staged as bytes rather than consistently.
# An archive whose manifest names the degradation is recoverable information; a quietly
# inconsistent database in an archive that reports success is not.
SKIP_DB_UNPINNED_SOURCE = "db_unpinned_source"


def _copy_database_consistently(
    src: Path,
    dst: Path,
    *,
    root: Path,
    rel_parts: tuple[str, ...],
    require_database: bool = False,
) -> str:
    """Copy the live SQLite database *src* to *dst* through the backup API, read-only.

    THE one place this module reads a live database on the creation path. Both staging
    paths -- the fixed core-file list and the tree re-stage pass -- call it, because the
    hardening below was arrived at over five review rounds on #5156 and a second,
    structurally identical copy of it is how one of the two drifts back.

    Four properties, each closing a failure that reported success:

    **The chain is verified through descriptors, not by name.** Every component from
    *root* down is opened relative to the previous one with ``O_NOFOLLOW``, so a directory
    that is a link -- or one swapped for a link while this runs -- fails its own open
    instead of redirecting the read. ``src.resolve()`` and a late ``realpath()`` are both
    a SECOND by-name walk and were both wrong here: they can land on a database this
    function never inspected, whose rows then ride in the bundle under an innocuous name.

    **The URI is percent-escaped.** ``as_uri()``, not interpolation: a POSIX filename
    containing ``?`` or ``#`` was otherwise parsed as the start of the URI's query or
    fragment, truncating the path so the copy opened a DIFFERENT database and stored it
    under the requested name. Built once and shared by the probe and the copy, because two
    spellings of one URI is how they diverge.

    **The connection is ``mode=ro``.** Staging only ever needs to read, and a read-write
    open is not merely more authority than required -- it MUTATES the live database.
    Measured on a fixture with a ``-wal`` left unreplayed by a killed writer: the
    read-write open recovered the log into the main file (8192 -> 16384 bytes, different
    hash) and unlinked the 836 KB ``-wal``; ``mode=ro`` left both byte-identical and
    captured exactly the same rows. So a backup command was rewriting the data it was
    asked to read, and in the window before SQLite's own open a swapped-in database was
    handed a writable handle. ``mode=ro`` refuses the write outright.

    **"Not a database" is told apart from "cannot read this database".** They are
    distinguished by probing readability separately from copying, not by matching the
    error, because ``sqlite_errorname`` is 3.11+ while this package supports 3.10 and
    message text changes with any SQLite release. The broad form was wrong in the
    dangerous direction: "database is locked" is also a ``DatabaseError``, so an exclusive
    writer made the probe report "not a database" and a raw byte copy shipped as if it
    were consistent.

    What this does NOT close, stated rather than implied: SQLite's API takes a PATH and
    cannot be pointed at a held descriptor -- probed, it refuses ``/proc/self/fd/N`` and
    ``/dev/fd/N`` alike -- so SQLite re-resolves the final name itself, and a same-uid
    swap in the window between the check above and that open is not detectable here. A
    post-hoc identity re-check does not close it either, since swapping back defeats the
    check. Closing it needs a descriptor-taking VFS, which is a different change.

    The WAL question the issue this came from also raises is NOT handled by checkpointing
    and copying bytes, and deliberately so: the backup API already reads a consistent
    snapshot that INCLUDES rows living only in the ``-wal``. Measured against a
    cross-process writer with ``wal_autocheckpoint=0`` and a 1.5 MB log, the copy
    contained all 421 rows -- 371 of them WAL-resident -- and passed ``integrity_check``.
    A check-then-copy-bytes design has the race; this one has no check to race.
    """
    if not _chain_is_link_free(root, rel_parts):
        if require_database:
            # Same reasoning as the not-a-database case below, and an earlier revision of
            # this change applied it to only one of the two: omitting a REQUIRED database
            # still let the snapshot succeed, so `--keep N` pruned the last complete
            # archive in favour of one missing the database it claims. A recorded omission
            # is "recoverable information" only while the operator still HAS the archive it
            # could be recovered from. Review's finding.
            raise DatabaseCopyFailed(
                src,
                pinned_fs.PinnedPathRefusal(
                    f"{'/'.join(rel_parts)} could not be verified as reachable without "
                    "following a link, so it was not read"
                ),
            )
        return DB_UNSAFE_SOURCE
    ro_uri = f"{src.absolute().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(ro_uri, uri=True)) as probe:
            probe.execute("PRAGMA schema_version").fetchone()
    except sqlite3.DatabaseError as e:
        # `sqlite_errorname` is read defensively for 3.10 and the message is the
        # documented fallback. Either way the DEFAULT is to raise: an error this code
        # cannot classify is not evidence the file is safe to copy byte-for-byte.
        name = getattr(e, "sqlite_errorname", "")
        not_a_database = name == "SQLITE_NOTADB" or (
            not name and "not a database" in str(e).lower()
        )
        if not not_a_database:
            raise DatabaseCopyFailed(src, e) from e
        if require_database:
            # A DECLARED component file, so this is a hard failure. The asymmetry with the
            # tree pass below is deliberate, and an earlier revision of this change got it
            # backwards by "unifying" the two -- review caught the consequence:
            #
            # A corrupt `memory.db` staged as bytes makes the snapshot SUCCEED. `--keep N`
            # then counts that archive as the newest backup and prunes a real one, while
            # restore refuses the new archive outright at its strict database validation.
            # The operator is left with no restorable backup, having run a command that
            # printed success. The module already names this hazard class where `--keep`
            # is handled: an empty bundle "would count as the newest backup and prune a
            # real one".
            #
            # A `.db` found by the TREE walk is incidental -- some file the operator
            # happens to keep in their workspace -- and refusing the whole snapshot over
            # it would be an outage, not a safeguard. Declared and load-bearing versus
            # discovered and incidental is a real difference, so the two paths get
            # different answers on purpose.
            raise DatabaseCopyFailed(src, e) from e
        return DB_NOT_A_DATABASE
    # The connects are INSIDE the try, not just `backup()`. Between the probe above and
    # this open the file can disappear -- and `mode=ro` makes that a hard failure where a
    # read-write open would have silently CREATED an empty database, so this change made
    # the window matter more, not less. `sqlite3.connect` then raises `OperationalError`,
    # which `snapshot_main` does not catch (it handles PinnedPathRefusal,
    # UnsafeComponentRoot, DatabaseCopyFailed and _ArchiveTooLarge), so the command exited
    # on a traceback instead of naming the database. Review's finding.
    try:
        with (
            closing(sqlite3.connect(ro_uri, uri=True)) as src_conn,
            closing(sqlite3.connect(str(dst))) as dst_conn,
        ):
            # The file is a readable database, so a failure here means the consistent copy
            # did not happen. Absorbing it would leave the caller's byte copy -- taken
            # WITHOUT the `-wal` this module excludes -- passing for a whole database, so
            # it is raised, but typed and naming the file so the command boundary reports
            # which database failed instead of exiting on a traceback.
            src_conn.backup(dst_conn)
    except sqlite3.Error as e:
        raise DatabaseCopyFailed(src, e) from e
    if require_database:
        _refuse_unsound_required_capture(src, dst)
    return DB_COPIED


def _refuse_unsound_required_capture(src: Path, dst: Path) -> None:
    """Raise unless a REQUIRED database was captured soundly.

    ``PRAGMA schema_version`` above only proves the file parses, which is a much weaker
    claim than restore's, and the two unsoundnesses it misses need checking at OPPOSITE
    ends. Both were measured rather than reasoned about.

    **Page corruption -- checked on the STAGED COPY.** With a database's interior pages
    overwritten and the header left intact, ``schema_version`` answered normally,
    ``backup()`` succeeded and faithfully staged all 192512 bytes, and ``integrity_check``
    reported "database disk image is malformed" on the source AND the copy. So the archive
    reported success and restore was guaranteed to refuse it; ``--keep N`` then counts it as
    the newest backup and prunes a real one, so the operator loses their last restorable
    copy at the one moment they reach for it. The copy is the right end to check: it is what
    goes in the archive and what restore validates, checking it keeps this path read-only
    with respect to live data, and it also catches a copy damaged in transit. Source
    corruption still surfaces, because ``backup()`` reproduces it.

    **A zero-byte source -- checked on the SOURCE, and ONLY visible there.** SQLite opens a
    zero-byte file as a valid EMPTY database, so ``integrity_check`` answers ``ok``. Worse,
    ``backup()`` does not preserve the emptiness: measured, a 0-byte source produced a
    4096-byte staged copy that ``integrity_check`` called ``ok``. Restore's own zero-byte
    guard reads the size of the ARCHIVED file, so it sees 4096 and accepts -- meaning that
    for this path restore does NOT refuse the archive, it RESTORES it and installs an empty
    database over live data while reporting success. That is why the check cannot be
    deferred to the copy or to restore: the "captured nothing" condition exists only at the
    source, which is the same reason restore reads size before opening.

    The cost is not a reason to hesitate: ``integrity_check`` measured 1285 MB/s against
    415 MB/s for the ``backup()`` and 227 MB/s for the gzip this command already performs
    over the same bytes unconditionally, so it adds about a tenth of work already paid.

    Raises ``DatabaseCopyFailed`` rather than restore's ``SourceComponentUnsound``: the try
    around ``_build_snapshot`` handles ``PinnedPathRefusal``, ``UnsafeComponentRoot`` and
    ``DatabaseCopyFailed``, so the restore-side type would leave here as a traceback --
    the same escape this change fixed for the chain check. Review's finding.
    """
    try:
        if src.stat().st_size == 0:
            raise DatabaseCopyFailed(
                src,
                sqlite3.DatabaseError(
                    "the live database is EMPTY (zero bytes). SQLite opens such a file as "
                    "a valid empty database and the staged copy passes an integrity check, "
                    "so this would archive nothing and a later restore would install "
                    "nothing over live data while reporting success"
                ),
            )
    except OSError as e:
        raise DatabaseCopyFailed(src, e) from e
    try:
        with closing(sqlite3.connect(str(dst))) as check:
            result = check.execute("PRAGMA integrity_check;").fetchone()[0]
    except sqlite3.Error as e:
        # Severe corruption makes the pragma RAISE rather than answer -- the measurement
        # above got `DatabaseError: database disk image is malformed` here -- so this arm is
        # the common path for a page-corrupt database, not a defensive afterthought.
        raise DatabaseCopyFailed(src, e) from e
    if result != "ok":
        raise DatabaseCopyFailed(
            src, sqlite3.DatabaseError(f"integrity check on the staged copy failed ({result})")
        )


def _restage_databases(
    src_dir: Path,
    dst_dir: Path,
    *,
    bundle_root: Path,
    on_skip: pinned_fs.SkipReporter | None = None,
) -> None:
    """Re-copy every SQLite database under *src_dir* through the backup API.

    The plain tree copy already placed a byte copy there; this replaces it with a
    consistent one. Done as a second pass rather than by filtering the tree walk, so
    the copy logic stays in one place and a database newly appearing in a tree is
    covered without anyone remembering to register it.

    A file whose suffix says database but which SQLite cannot open is left as the byte
    copy already made -- UNLESS its bundle-relative path is in ``PRODUCT_TREE_DATABASES``,
    in which case the snapshot fails. That set's own contract is that everything in it is
    "validated as strictly as ``memory.db``", and the restore side already enforces exactly
    that (``_refuse_unless_sound(..., strict=rel in PRODUCT_TREE_DATABASES)``). Staging a
    corrupt ``workspace/knowledge/knowledge.db`` as bytes and reporting success therefore
    produced an archive that restore is guaranteed to refuse, which is worse than failing:
    ``--keep N`` counts the new archive as the newest backup and prunes a real one, so the
    operator loses their last restorable copy to a snapshot that "succeeded". Review's
    finding.

    *bundle_root* is what makes that key comparable. ``PRODUCT_TREE_DATABASES`` is spelled
    relative to a bundle root, while this pass walks one tree, so a tree-relative path would
    never match any entry and the strictness would be silently vacuous.

    A non-database that happens to be named ``.db`` and is NOT one of ours is still the
    operator's file and must ride the bundle: refusing a whole snapshot over a stray
    ``Thumbs.db`` would be an outage rather than a safeguard.

    A database the tree copy did NOT stage is left alone. This pass exists to REPLACE a
    byte copy with a consistent one, so a destination that does not already hold that byte
    copy means the walk deliberately declined the source -- a hardlink alias, a symlink, a
    non-regular entry -- and recreating it here from the source inode would reinstate
    exactly what the walk refused. Reproduced before this guard existed: a `.db` hardlinked
    to a database outside the component tree was skipped as `not_regular` and then rebuilt
    by this pass, putting the external database's rows in the bundle. Checking the PARENT
    directory is not enough, because the parent exists for every sibling that copied fine.

    *on_skip* is how a degradation reaches ``MANIFEST.json``. A source that cannot be
    verified link-free leaves the tree walk's byte copy in place, which is the right call
    -- deleting it is data loss -- but the bundle then holds a database that was never
    copied consistently, and that belongs on the record rather than only in the console
    scrollback of whoever ran the command.
    """
    for src in sorted(src_dir.rglob("*")):
        if not src.is_file() or src.is_symlink() or src.suffix not in _DB_SUFFIXES:
            continue
        dst = dst_dir / src.relative_to(src_dir)
        # `lstat`, not `exists()`: the latter follows a link, so a link planted at the
        # destination name would answer for its target and be treated as a staged copy.
        try:
            dst_st = dst.lstat()
        except OSError:
            continue
        if not _stat.S_ISREG(dst_st.st_mode):
            continue
        # Spelled `relative_to(bundle_root).as_posix()` to match the restore side's own
        # key for the same set, so the two ends of the invariant read the same.
        rel = dst.relative_to(bundle_root).as_posix()
        outcome = _copy_database_consistently(
            src,
            dst,
            root=src_dir,
            rel_parts=src.relative_to(src_dir).parts,
            require_database=rel in PRODUCT_TREE_DATABASES,
        )
        if outcome == DB_UNSAFE_SOURCE:
            # The byte copy the walk already made stays -- it is the operator's data and
            # this pass only ever REPLACES a copy, never creates one. Recorded so the
            # archive does not silently claim a consistent database.
            if on_skip is not None:
                on_skip(SKIP_DB_UNPINNED_SOURCE, str(src))
        elif outcome == DB_NOT_A_DATABASE:
            print(
                f"⚠️  {_safe_name(src.name)} is not a readable SQLite database "
                "— copied as a plain file"
            )


def safe_tree_root(root: Path, *, what: str, home: Path | None = None) -> Path | None:
    """Return *root* if it is the declared tree AND staying inside the data home.

    THE chokepoint for component tree roots. Three separate sites touch them — the
    staging walk, the replace pass and the merge pass — and each was found to
    dereference a link independently, so the check lives here once.

    Two INDEPENDENT properties are required, and neither implies the other:

    **Containment** — the fully resolved path is a strict descendant of the resolved
    home. This answers "can a read or write through this root land outside the
    directory we are allowed to touch". Checking whether the node itself is a link
    does not answer it: a link nested under the root, or an ancestor of it, escapes
    while every individual node looks ordinary. ``Path.resolve()`` follows every link
    in the path, so the comparison covers roots, ancestors, descendants and Windows
    junctions at once. Equality with the home is refused, not allowed: a link like
    ``workspace/memory -> ..`` resolves to the home itself, which would make the
    "component tree" the whole home and sweep ``.env`` and ``sel_hmac.key`` into an
    archive meant to carry memory. No declared component tree is ever the home.

    **Identity** — no path segment from the home down to the root is a link. This
    answers a different question: "is this the tree the component declared". A link
    that redirects to another subtree INSIDE the home satisfies containment perfectly
    — ``workspace/memory -> ../apps`` resolves to a strict descendant — while
    silently changing WHICH data is archived. Because these bundles are uploaded, a
    redirect is an exfiltration primitive, not a mix-up: the archive would carry
    whatever the link points at under the name of the component that was asked for.
    Containment cannot see this, because nothing left the home.
    """
    base = (home or _mc_dir()).resolve()
    try:
        resolved = root.resolve()
    except OSError as e:  # broken link, ELOOP, permission on an ancestor
        print(f"⚠️  Skipping unresolvable {what} ({e}): {root}")
        return None
    if base not in resolved.parents:
        print(f"⚠️  Skipping {what} that resolves outside {base}: {root} -> {resolved}")
        return None
    # Identity. Walk the segments BELOW the home only: the home itself is allowed to
    # sit behind a link (a real one often does), and resolving it once already accounted
    # for that. Climbing stops as soon as a parent resolves to the home, so a link above
    # the home is never mistaken for a redirect within it.
    probe = root.absolute()
    while True:
        if platform_compat.is_link_or_junction(probe):
            print(
                f"⚠️  Skipping {what} that is reached through a link: {probe}. "
                "A component tree must be the declared directory, not a redirect to "
                "another one — the archive is uploaded, so a redirect would ship "
                "whatever the link points at."
            )
            return None
        parent = probe.parent
        if parent == probe:
            break
        try:
            if parent.resolve() == base:
                break
        except OSError:
            break
        probe = parent
    return root


def _fsize(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _want(components: list[str] | None, name: str) -> bool:
    return components is None or name in components


def _list_components() -> None:
    print("Available components:")
    for k, v in COMPONENT_HELP.items():
        print(f"  {k:16s} {v}")
    print("\nCombine with commas: --components memory,crons,skills")


class RollbackIncomplete(OSError):
    """The restore failed AND putting the previous state back did not fully succeed.

    Distinct from the restore failure itself, because the two need opposite messages: one
    says "you are back where you started", the other says "some of your previous state is
    only in the rollback directory now". Reporting the first when the second is true is
    the worst of the three outcomes -- the operator stops looking.
    """

    def __init__(self, cause: BaseException, failed: list[str], backup: "Path") -> None:
        self.cause = cause
        self.failed = failed
        self.backup = backup
        super().__init__(str(cause))


class UnsafeComponentRoot(Exception):
    """A selected component's tree root does not resolve inside the data home.

    Raised rather than skipped: a bundle whose manifest claims a component it could not
    read is a backup that lies about its contents, which is worse than a refusal.
    """


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


def _terminal_safe(value: object) -> str:
    """Render *value* so an untrusted string cannot drive the terminal.

    A restore accepts an arbitrary ``.tar.gz``, so every string that comes back out of one
    -- a manifest field, a member name -- is attacker-controlled input being written to a
    terminal. ANSI and OSC sequences in it are executed by the terminal, not displayed, so
    a crafted archive can rewrite what the operator appears to be reading, or worse.
    Raised in review against the omission list this change added.

    Control characters are escaped rather than stripped, so the value stays diagnosable
    (an operator can see the file really is named with an escape) instead of silently
    reading as a different, innocuous name. ``str.isprintable()`` is False for exactly the
    C0/C1 range plus the separators, and True for ordinary text in any language, so a
    non-ASCII path is unharmed.
    """
    return "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in str(value))


def _report_skip(reason: str, path: str) -> None:
    """Word a primitive's skip classification in this module's existing voice.

    The primitive classifies and never prints, so these strings stay byte-identical
    to what snapshot/restore printed before the migration.

    The path is rendered through :func:`_terminal_safe` because on the RESTORE side these
    names come out of the archive: the walk is over an extracted tree whose member names
    the archive chose, and `_data_filter` screens traversal, not escape bytes.
    """
    safe = _terminal_safe(path)
    if reason == pinned_fs.SKIP_SYMLINK:
        print(f"⚠️  Skipping symlink in source tree: {safe}")
    elif reason == pinned_fs.SKIP_VANISHED:
        print(f"⚠️  Skipping vanished entry during snapshot copy: {safe}")
    else:
        print(f"⚠️  Skipping hardlinked or non-regular file during snapshot copy: {safe}")


def _staging_is_pinned(*, allow_unpinned: bool, what: str) -> bool:
    """Whether staging may proceed, and whether it will be descriptor-pinned.

    Returns True for a pinned traversal, False for the by-name traversal the caller
    explicitly asked for. Raises rather than returning False when the platform cannot
    pin and no one said that is acceptable.

    This is the whole "refuse rather than fall back" rule, in one place. The reason it
    is a refusal and not a warning: a by-name walk is not a slightly weaker version of
    a pinned walk, it is the mechanism whose failure closed two pull requests. An
    operator who needs a snapshot on a platform without ``dir_fd`` can still have one,
    but they say so on the command line and the archive records that they did, so the
    weaker mode is never something the tool chose on their behalf.
    """
    if pinned_fs.supports_pinned_tree_walk():
        return True
    if allow_unpinned:
        return False
    raise pinned_fs.PinnedPathRefusal(
        f"refusing to stage the {what}: this platform cannot open a directory "
        "relative to a descriptor, so every component would be re-opened by name and "
        "an ancestor swapped mid-walk could redirect the copy into a credential "
        "store. Re-run with --allow-unpinned-staging to accept a by-name traversal; "
        "the archive will record that it was staged unpinned."
    )


def _copytree_safe(
    src: Path,
    dst: Path,
    *,
    allow_unpinned: bool = False,
    on_skip: pinned_fs.SkipReporter | None = None,
    must_create: bool = False,
    **kwargs,
) -> None:
    """Copy a tree for staging, with the source traversal pinned where possible.

    Was: ``shutil.copytree`` with an ignore callback that tested ``os.path.islink`` on
    a NAME. That screened the final component of each entry and nothing else, so an
    ancestor directory swapped for a link between the listing and the copy redirected
    every deeper open, and the screen had nothing to report -- what it found inside
    the replaced tree was an ordinary file. Now the traversal is descriptor-pinned by
    :func:`kiro_crew.pinned_fs.stage_tree_pinned`, including the chain above the root.

    ``dirs_exist_ok`` is still accepted and dropped -- it was a ``shutil.copytree``
    keyword and callers may pass it out of habit -- but it is no longer meaningless
    generally: whether an existing destination is tolerated is now decided by
    *must_create*, and it is decided identically on both branches. Every other keyword is
    rejected rather than silently dropped.

    *must_create* says the caller REPLACES its destination rather than merging into it, so
    a root that exists is refused. Only a caller that removed the tree it is about to write
    knows this, so it is passed, never derived -- deriving it from ``skip_existing`` refused
    every snapshot, because the snapshot's own staging root already exists.

    *on_skip* lets a caller both print and RECORD what was skipped. It defaults to
    printing only, which is right for restore; the snapshot path passes a recorder so
    an incomplete archive says so in its own manifest instead of only in the console
    output of whoever ran it.
    """
    report = on_skip or _report_skip
    outer_ignore = kwargs.pop("ignore", None)
    kwargs.pop("dirs_exist_ok", None)
    if kwargs:
        raise TypeError(f"_copytree_safe got unexpected keyword arguments: {sorted(kwargs)}")

    if _staging_is_pinned(allow_unpinned=allow_unpinned, what=f"tree {src.name!r}"):
        pinned_fs.stage_tree_pinned(
            src,
            dst,
            what=f"tree {src.name!r}",
            ignore=outer_ignore,
            on_skip=report,
            must_create=must_create,
        )
        return

    # Declared by-name traversal. The TRAVERSAL is the weakness the operator opted into --
    # an ancestor swapped mid-walk can still redirect it, and nothing here can prevent
    # that without the descriptor support the platform lacks. The PER-FILE screens are a
    # different matter and review was right that they had been left behind: plain
    # `copytree` dereferences a hardlink into ordinary bytes and follows a Windows
    # junction, so a credential aliased into a staged tree would have ridden along even
    # though the pinned path refuses exactly that. Each file now goes through
    # copy_file_pinned (same fstat screens, minus the pinned ancestors) and the screen
    # rejects reparse points, which `islink` alone does not report on Windows.
    def _ignore_unsafe(directory, contents):
        skipped = set()
        for entry in contents:
            full = os.path.join(directory, entry)
            if os.path.islink(full) or pinned_fs.is_reparse_point(full):
                skipped.add(entry)
                report(pinned_fs.SKIP_SYMLINK, full)
        if outer_ignore:
            skipped |= set(outer_ignore(directory, contents))
        return skipped

    def _copy_screened(source: str, target: str, **_kw) -> None:
        pinned_fs.copy_file_pinned(source, target, on_skip=report)

    # `dirs_exist_ok` has to follow `must_create`, not be hardcoded. Review found the gap:
    # `must_create` reached the pinned walk and stopped there, so on a platform that cannot
    # pin -- Windows, the dashboard's replace -- a root recreated after the rmtree was still
    # merged into and stale files survived a successful replace. That is the same mistake as
    # the earlier Windows import refusal in this PR: a guard added to the pinned path and not
    # carried to the by-name one. Every mutating path gets the gate or the gate is decorative.
    try:
        shutil.copytree(
            str(src),
            str(dst),
            ignore=_ignore_unsafe,
            copy_function=_copy_screened,
            dirs_exist_ok=not must_create,
        )
    except FileExistsError as exc:
        # Same refusal type and same sentence as the pinned branch, so a caller has one
        # thing to contain and the operator reads the same explanation on either platform.
        raise pinned_fs.PinnedPathRefusal(
            f"refusing to use the tree {src.name!r} destination: {dst.name!r} already "
            "exists, and this operation replaces its destination rather than merging "
            "into it. Something recreated that directory after it was removed, so "
            "staging into it would leave files the archive does not contain while "
            "reporting a replacement. Remove it and re-run with the gateway stopped."
        ) from exc


def _copy_tree_no_overwrite(src: Path, dst: Path, *, allow_unpinned: bool = False) -> None:
    """Merge *src* into *dst* without overwriting, with both ends pinned.

    The destination side is where #3797's third finding lives. The previous version
    walked the source with ``rglob`` and wrote each file with ``shutil.copy2`` to a
    path composed by name, so the destination's ancestor chain was never pinned: a
    component of *dst* swapped for a link after ``mkdir`` redirected the write, and
    ``not target.exists()`` answered for whatever the link pointed at rather than for
    the directory the caller validated.

    This is now one call into the shared primitive with ``skip_existing=True``, which
    is what makes the no-overwrite promise real: exclusive creation is atomic, so "it
    did not exist a moment ago" and "this call created it" are the same statement
    rather than two with a window between them.

    An earlier revision open-coded a second pinned walk here, with its own copy body.
    Review pointed out the two had already diverged -- this one's child-directory open
    lacked the ``ELOOP``/``ENOTDIR`` handling, so the very swap the staging walk skips
    would have escaped restore as a raw ``OSError`` -- which is the argument for a
    parameter on one primitive rather than a parallel implementation the shared
    module's own docstring says should not exist.
    """
    if not _staging_is_pinned(allow_unpinned=allow_unpinned, what=f"restore of {dst.name!r}"):
        for item in src.rglob("*"):
            if item.is_symlink():
                continue
            target = dst / item.relative_to(src)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                # `copy2` opened the destination BY NAME for writing, so a symlink planted
                # at that name after the `not target.exists()` check was followed and an
                # arbitrary external file was overwritten. Review's finding, and the
                # `exists()` guard was itself the name-based check that created the window.
                #
                # copy_file_pinned opens the destination O_CREAT|O_EXCL|O_NOFOLLOW even
                # with no directory descriptor, so the link is refused rather than
                # followed, and O_EXCL subsumes the skip-if-present behaviour the old
                # `not target.exists()` was there to provide -- without the race.
                pinned_fs.copy_file_pinned(
                    str(item), str(target), skip_existing=True, on_skip=_report_skip
                )
        return

    pinned_fs.stage_tree_pinned(
        src,
        dst,
        what=f"restore of {dst.name!r}",
        on_skip=_report_skip,
        skip_existing=True,
    )


# ── Snapshot ──────────────────────────────────────────────────────────────────


class ManifestUnreadable(Exception):
    """A bundle's manifest exists but cannot be trusted to say what it carries."""


class _ArchiveTooLarge(Exception):
    """An archive declares more content than a memory bundle can justify."""


class SourceComponentUnsound(Exception):
    """An incoming component in a bundle is unsound, so nothing may be restored from it.

    Covers both kinds of unsoundness this path can detect before mutating: a database
    that fails its integrity check, and a component JSON whose reader would treat it as
    empty. Both share one boundary handler because both mean the same thing to the
    operator — the bundle cannot be applied — and neither should surface as a traceback.

    Raised before any live state moves, because the point of the check is that it still
    costs nothing to decline.
    """


# Generous next to a real memory bundle (megabytes, a few thousand members) and still
# far below what would fill a disk. Both bounds are needed: total size alone misses an
# archive whose damage is a huge member COUNT, and count alone misses one member that
# declares a terabyte.
_MAX_ARCHIVE_MEMBERS = 200_000
_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024


def _refuse_oversized_archive(probe: tarfile.TarFile) -> None:
    """Refuse an archive that would not fit, before anything is extracted.

    A compressed archive can declare orders of magnitude more content than it occupies,
    so size on disk says nothing about what extraction would write. The check has to run
    against the member headers, and it has to run BEFORE ``extractall``: once extraction
    starts, the damage is already on the filesystem.

    Applied on every path that reads an archive — staging a bundle for upload, a bundle
    fetched from object storage, and a local bundle handed to `restore`. A local file is
    not trustworthy by virtue of being local; it can be hostile or simply wrong.

    Members are walked one at a time rather than through ``getmembers()``, because
    materialising the whole index is itself the denial of service an archive with
    millions of members performs. Bailing on the member that crosses the bound means the
    work is bounded by the bound, not by what the archive claims.
    """
    total = 0
    count = 0
    while (member := probe.next()) is not None:
        count += 1
        if count > _MAX_ARCHIVE_MEMBERS:
            raise _ArchiveTooLarge(
                f"This archive declares more than {_MAX_ARCHIVE_MEMBERS:,} "
                "entries, which no memory bundle produces"
            )
        # Only regular files carry payload; a directory or link header declares a size
        # that extraction never writes, so counting those would refuse honest archives.
        if member.isfile():
            total += max(member.size, 0)
            if total > _MAX_ARCHIVE_BYTES:
                raise _ArchiveTooLarge(
                    "This archive declares more than "
                    f"{_MAX_ARCHIVE_BYTES // (1024 ** 3)} GiB of uncompressed content, "
                    "which no memory bundle produces"
                )


def _manifest_components(snap: Path) -> list[str] | None:
    """Return the component names a bundle's manifest says it carries.

    ``None`` means "this bundle predates the component map", which is the signal to
    keep the historical all-components behaviour — such a bundle really did hold every
    component. That fallback is reserved for a manifest that is READABLE and simply
    has no map; a manifest that cannot be parsed raises :class:`ManifestUnreadable`
    instead, because "we could not read it" must never resolve to the most destructive
    interpretation available.

    Names not in :data:`COMPONENTS` are dropped: the manifest travels with the bundle,
    so a restore must not act on a name this build cannot resolve. The remaining list
    is returned even when EMPTY — that means "declares components, none understood
    here", which must restore nothing.
    """
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return None
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise ManifestUnreadable(f"MANIFEST.json is present but unreadable: {e}") from e
    if not isinstance(manifest, dict):
        raise ManifestUnreadable("MANIFEST.json is not an object")
    comps = manifest.get("components")
    if comps is None:
        return None
    if not isinstance(comps, dict):
        raise ManifestUnreadable(f"MANIFEST.json 'components' is {type(comps).__name__}, not a map")
    known = [c for c in comps if c in COMPONENTS]
    dropped = sorted(set(comps) - set(known))
    if dropped:
        print(
            "⚠️  Manifest names unknown component(s), ignoring: "
            + ", ".join(_safe_name(d) for d in dropped)
        )
    return known


def _trees_absent_from_bundle(snap: Path, names: list[str], mc: Path) -> list[str]:
    """Return the declared trees of *names* that *snap* lacks while *mc* HAS them.

    Split from the per-component check because the two answer different questions, and
    conflating them is what let live data go. Per COMPONENT, presence is "any declared path
    is there", which is right: a home that never wrote ``memory_index.db`` produces a memory
    bundle with only ``memory.db``, and demanding every file would refuse a sound bundle.
    Per TREE under ``--mode replace`` that leniency is destructive, because
    :func:`_replace_tree_root` clears the destination BEFORE it knows whether the archive
    has a replacement -- so one present file makes the whole component look carried while
    an absent tree is cleared from live state and the restore reports success.

    Both halves of the condition are load-bearing, and requiring only the first was wrong:
    a home that never used a tree produces a bundle without it, so "the archive lacks this
    tree" alone refuses sound bundles. What makes it a LOSS is live state to lose. Naming
    the live side keeps the refusal to exactly the case where clearing destroys something.

    Deliberately not applied to a complete bundle: there, a tree the archive lacks is a
    tree the source genuinely did not have, so clearing it is the point of replace. Only a
    bundle that ASSERTS it is partial while carrying no component map cannot tell those two
    apart, and that is the one case this speaks for.
    """
    absent = []
    for name in names:
        for tree in COMPONENTS[name].trees:
            d = mc / tree
            # Through the chokepoint like every other tree-root site. A live root that
            # redirects elsewhere, or escapes the home, is not the tree this component
            # declared, so it must not count as state worth refusing to protect -- and
            # deciding that from `is_dir()` alone would follow the link.
            if safe_tree_root(d, what="destination root", home=mc) is None:
                continue
            if not (snap / tree).is_dir() and d.is_dir():
                absent.append(tree)
    return absent


def _components_absent_from_bundle(snap: Path, names: list[str]) -> list[str]:
    """Return the *names* whose declared paths are all missing from *snap*.

    The manifest is normally what answers "does this bundle carry X", and a
    bundle that declares a component map is checked against it. This is the
    fallback for the one shape that has no map to check: a root marked partial
    whose manifest predates (or omits) the component list.

    Naming a component is not evidence the bundle holds it. Replace mode moves
    each live core file aside before it knows whether the archive has a
    replacement, and clears a component tree whether or not the archive carries
    one -- so an operator who names a component the bundle never held loses that
    component and is told the restore succeeded.

    Presence is "any declared path is there", not "all of them". A component
    legitimately ships without every file: a home that never wrote
    ``memory_index.db`` produces a memory bundle with only ``memory.db``, and
    requiring both would refuse a sound bundle.
    """
    absent = []
    for name in names:
        spec = COMPONENTS[name]
        carried = any((snap / f).is_file() for f in spec.files) or any(
            (snap / t).is_dir() for t in spec.trees
        )
        if not carried:
            absent.append(name)
    return absent


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

    # Every skip is recorded, not just printed. A snapshot that omitted a hardlinked
    # or symlinked file used to report success with a console warning and nothing in
    # the archive -- the same "silent partial" shape this change fixes on the restore
    # side, raised in review. Paths are stored relative to the data home so the record
    # names the file without carrying the absolute layout of the machine into an
    # archive that may be moved somewhere else.
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
                        # this database". Before this, the core path was the last
                        # unhardened SQLite read on the creation path -- it connected
                        # READ-WRITE to the live name, which is what #5451 is about.
                        #
                        # `mc_fd` screened this name a few lines up and cannot be handed
                        # to SQLite, which takes only a path; the chain check inside the
                        # helper is what makes the ANCESTORS non-redirectable, and the
                        # residual final-name window is documented there.
                        # `require_database=True` makes every non-success outcome a raise,
                        # so there is no outcome to branch on here: a declared component
                        # file is either copied consistently or the snapshot fails. Both
                        # degradations that used to live here -- byte-copying a corrupt
                        # database, and omitting an unverifiable one -- let the command
                        # succeed, which let `--keep` prune the last good archive in favour
                        # of one that cannot be restored.
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
            if not src_dir.is_dir():
                continue
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            _copytree_safe(
                src_dir,
                dst_dir,
                allow_unpinned=allow_unpinned,
                on_skip=_record_skip,
                ignore=shutil.ignore_patterns(
                    "hygiene_data", "insert_facts*.py", *_DB_SIDECAR_GLOBS
                ),
            )
            # A tree can contain a LIVE SQLite database (workspace/knowledge holds
            # knowledge.db, whose WAL is routinely megabytes). A filesystem copy
            # reads the db and its sidecars at different instants, so a concurrent
            # write yields a restored database missing committed rows or corrupt
            # outright. Re-copy each one through the backup API, which takes a
            # consistent snapshot, and leave the -wal/-shm out entirely: they
            # describe the source's transaction state, not the copy's.
            _restage_databases(src_dir, dst_dir, bundle_root=stage, on_skip=_record_skip)

        # Manifest
        ws_files = sum(1 for _ in (stage / "workspace").rglob("*") if _.is_file())
        pm_files = sum(1 for _ in (stage / "plan_memory").rglob("*") if _.is_file())
        sk_dir = stage / "skills"
        sk_count = sum(1 for _ in sk_dir.iterdir() if _.is_dir()) if sk_dir.is_dir() else 0
        # Recorded so a reader can tell how the archive was built. "unpinned" means
        # the trees were walked by name, which an ancestor swap during staging could
        # have redirected. Someone deciding whether to trust this archive needs that
        # on the record rather than in the memory of whoever ran the command.
        staging_mode = "pinned" if pinned else "unpinned"
        manifest = {
            "version": 3,
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
            },
        }
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
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
            # Lock the archive down BEFORE it is published. Carried over from main's
            # #5317 during the rebase: this block was extracted out of snapshot_main
            # into this function, and main had meanwhile moved the lockdown to before
            # the rename, so taking this side of the conflict wholesale would have
            # silently dropped that fix.
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
    complete = set(selected) == set(COMPONENTS)
    name = f"kirocrew-snapshot-{ts}"
    root_name = name if complete else f"kirocrew-partial-{ts}"

    # Pre-flight size estimate
    if mc.is_dir():
        total_bytes = sum(
            f.stat().st_size for f in mc.rglob("*") if f.is_file() and not f.is_symlink()
        )
        total_mb = total_bytes / (1024 * 1024)
        if total_mb > 500:
            print(f"⚠️  {mc} is {total_mb:.0f} MB — snapshot may be large and slow")

    # NO pre-staging WAL checkpoint. There used to be one here, and removing it is the
    # point rather than an omission: it was the ONLY write this command made to the live
    # database, and it could not be made safe. A checkpoint cannot run read-only, so the
    # name has to be reopened for writing -- and verifying the name first does not close
    # that, because SQLite re-resolves it, so a swap in the window between the check and
    # the open put the checkpoint's WRITE into whatever the name then pointed at,
    # truncating an external database's log. Review's finding, and the remedy it asked for.
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
    for old in snaps[args.keep :]:
        old.unlink()
        print(f"🗑  Pruned: {_safe_name(old.name)}")

    remaining = len(list(out.glob("kirocrew-snapshot-*.tar.gz")))
    print(f"📦 Snapshots in {out}: {remaining} (keep={args.keep})")
    return 0


# ── Restore ───────────────────────────────────────────────────────────────────


def _print_manifest(snap: Path) -> None:
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
        print("📋 Snapshot info:")
        # Every string below comes out of an archive the caller supplied, so all of them
        # go through _terminal_safe. Review named the omission list this change added;
        # created_at, user and hostname are the same class and pre-date this diff. They
        # are fixed here rather than left as a matching hole three lines away, because
        # the renderer makes each one a one-word change and shipping a function that
        # sanitizes two of five attacker-controlled fields would be worse than either
        # extreme. Named as a drive-by rather than smuggled in.
        print(f"  Created: {_terminal_safe(m.get('created_at', 'unknown'))}")
        print(
            f"  From: {_terminal_safe(m.get('user', 'unknown'))}"
            f"@{_terminal_safe(m.get('hostname', 'unknown'))}"
        )
        c = m.get("contents", {})
        # Absent in bundles written before the purpose seam existed. Say so rather than
        # printing a default, so an old bundle is never read as a declared one.
        print(f"  Purpose: {_safe_name(m.get('purpose'), 'undeclared (pre-seam bundle)')}")
        comps = m.get("components")
        if isinstance(comps, dict) and comps:
            rendered = ", ".join(
                f"{_safe_name(k, '?')} [{_safe_name(v, '?')}]" for k, v in sorted(comps.items())
            )
            print(f"  Components: {rendered}")
        print(f"  Memory DB: {c.get('memory_db', 0) // 1024} KB")
        print(f"  Crons: {c.get('crons_json', 0) // 1024} KB")
        print(f"  Workspace files: {c.get('workspace_files', 0)}")
        print(f"  Skills: {c.get('skill_count', 0)}")
        print(f"  Notifications: {c.get('notifications_jsonl', 0) // 1024} KB")
        print(f"  Plan memory files: {c.get('plan_memory_files', 0)}")
        # Both of these are the record that makes an incomplete or weaker archive
        # visible. A value written but never displayed is only findable by untarring
        # the archive by hand, which is not a reader -- so they are shown here, where
        # anyone inspecting a snapshot before restoring it already looks.
        if m.get("staging") == "unpinned":
            print("  ⚠️  Staged by path name (unpinned): see --allow-unpinned-staging")
        for entry in m.get("skipped") or ():
            reason = _terminal_safe(entry.get("reason", "?"))
            omitted = _terminal_safe(entry.get("path", "?"))
            print(f"  ⚠️  Omitted ({reason}): {omitted}")
    except Exception as e:
        print(f"  (Could not read manifest: {e})")


_MERGE_ALLOWED_TABLES = frozenset(
    {
        "semantic_memory",
        "episodic_memories",
        "knowledge_facts",
        "knowledge_edges",
    }
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Validate a SQL identifier against allowlist pattern. Raises ValueError if invalid."""
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def _merge_memory(src_db: Path, dst_db: Path) -> None:
    # Integrity check on source DB before ATTACH.
    #
    # `closing`, not a bare `with sqlite3.connect(...)`: a connection used as a context
    # manager commits or rolls back the TRANSACTION and leaves the connection OPEN. The
    # handle it kept on src_db made the caller's extraction temp dir undeletable on
    # Windows, which is how this surfaced.
    try:
        with closing(sqlite3.connect(str(src_db))) as check_conn:
            result = check_conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if result != "ok":
            print(f"  ⚠️  Source DB integrity check failed: {result} — skipping merge")
            return
    except Exception as e:
        print(f"  ⚠️  Source DB unreadable: {e} — skipping merge")
        return

    conn = sqlite3.connect(str(dst_db))
    conn.execute("BEGIN")
    attached = False
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(src_db),))
        attached = True
        for table, cols, where in [
            (
                "semantic_memory",
                "key, value_json, confidence, source, created_at, updated_at, embedding",
                "WHERE is_deleted=0",
            ),
            (
                "episodic_memories",
                "id, conversation_id, text, embedding, tags, importance, created_at, last_accessed_at",
                "WHERE is_deleted=0",
            ),
            ("knowledge_facts", "subject, predicate, object, episode_id, created_at", ""),
            (
                "knowledge_edges",
                "source_key, target_key, relation, weight, metadata, created_at",
                "",
            ),
        ]:
            if table not in _MERGE_ALLOWED_TABLES:
                raise ValueError(f"Table {table!r} not in merge allowlist")
            for col in cols.split(", "):
                _validate_identifier(col.strip())
            try:
                before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({cols}) "
                    f"SELECT {cols} FROM src.{table} {where}"
                )
                after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                label = table.replace("_", " ").title()
                print(f"  {label} imported: {after - before}")
            except sqlite3.OperationalError as e:
                import logging

                logging.getLogger(__name__).warning("Skipping table %s: %s", table, e)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if attached:
            try:
                conn.execute("DETACH DATABASE src")
            except Exception:
                pass
        conn.close()


def _usable_cron_shape(parsed: object, path: Path) -> bool:
    """Refuse a crons file that parsed but is not shaped like a cron file.

    The merge looks ``jobs`` up on the result and calls ``.get`` on every
    entry, so valid JSON of the wrong shape -- a top level that is not an
    object, a ``jobs`` that is not a list, a job that is not an object, or a
    present name that is not encodable text -- would raise TypeError,
    AttributeError, or UnicodeEncodeError a line or two further down: the
    same crash the read guard exists to prevent, just moved. Only the structure the merge itself relies on is
    checked; the fields of a job are the cron loader's business, not this
    one's. A missing ``jobs`` key keeps its existing meaning of "no jobs".
    """
    if not isinstance(parsed, dict):
        print(f"  ⚠️  {path} is not a cron file — skipping cron merge")
        return False
    jobs = parsed.get("jobs", [])
    if not isinstance(jobs, list) or not all(
        isinstance(job, dict)
        and (
            "name" not in job
            or (
                isinstance(job["name"], str)
                # json.loads accepts lone-surrogate escapes, and a present name
                # is UTF-8 encoded when the import id is hashed: a surrogate
                # would raise UnicodeEncodeError there, the same crash moved.
                and not any("\ud800" <= ch <= "\udfff" for ch in job["name"])
            )
        )
        for job in jobs
    ):
        print(f"  ⚠️  {path} has an unusable job list — skipping cron merge")
        return False
    return True


def _merge_crons(src_path: Path, dst_path: Path) -> bool:
    """Merge the archive's cron jobs into the live store.

    Returns ``True`` when the merged store was written, ``False`` when the
    merge was refused: an unreadable source, an unreadable destination, or an
    unusable cron shape on either side. The refusal diagnostics stay on
    stdout, but a print is invisible to a caller with no terminal — the
    dashboard import reported "crons (merged)" over a refusal that imported
    zero jobs — so the outcome is also returned for the caller to report.
    A failing WRITE of the merged store is not a refusal: the ``OSError``
    propagates, which is the loud behavior the caller's error path expects.
    """
    # Cron job names are operator-authored text and routinely non-ASCII, so the
    # locale codepage is the wrong decoder for this file on any host.
    try:
        src = json.loads(src_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  Could not read {src_path}: {exc} — skipping cron merge")
        return False
    try:
        dst = json.loads(dst_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  Could not read {dst_path}: {exc} — skipping cron merge")
        return False
    if not _usable_cron_shape(src, src_path) or not _usable_cron_shape(dst, dst_path):
        return False
    existing = {j.get("name") for j in dst.get("jobs", [])}
    imported = 0
    for job in src.get("jobs", []):
        name = job.get("name")
        if not name or name in existing:
            continue
        job["id"] = hashlib.md5(f"{name}-imported".encode(), usedforsecurity=False).hexdigest()[:8]
        dst.setdefault("jobs", []).append(job)
        imported += 1
    dst_path.write_text(json.dumps(dst, indent=2), encoding="utf-8")
    total = len(src.get("jobs", []))
    print(f"  Cron jobs imported: {imported} (skipped {total - imported} duplicates)")
    return True


_TERMINATORS = (b"\n", b"\r")

# Longest single notification record either side of the merge will materialise.
# Both trees are agent-writable and the read feeds an append to a durable file,
# so an over-cap record aborts rather than being skipped. Named here, and read at
# call time, so a test can move the dial instead of writing a 128 MiB fixture --
# the same reason `subagent_cost` names its own.
_NOTIFICATION_RECORD_CAP = RECORD_CAP


def _notification_key(record: bytes, path: Path) -> tuple[Any, ...] | None:
    """The dedupe key for one raw notification record, or ``None`` if it has none.

    Well-defined and hashable for EVERY record shape, which the previous
    ``json.loads(line).get("ts") or line.strip()`` was not: a non-object
    record raised ``AttributeError`` off ``.get``, and a list or dict ``ts``
    raised ``TypeError`` on set insert. Both escaped as an aborted restore.

    A ``ts`` is used whenever it is truthy AND hashable, which is every JSON
    scalar. Restricting it to ``str`` would have been a REGRESSION: the
    predecessor keyed a numeric ``ts`` on the number, so two rows carrying the
    same numeric ``ts`` with different bytes -- one normalised, one not --
    deduplicated, and keying them on their raw form instead persists a
    duplicate. Only an unhashable ``ts``, which cannot be a set member at all,
    falls through to the raw form.

    The ``ts`` goes into the key under a KIND TAG that is deliberately coarser
    than its Python type, because two different equalities are in play at once
    and a naive tag gets one of them wrong:

    * ``True == 1`` and ``hash(True) == hash(1)``, so an untagged key makes a row
      with ``ts: true`` and a row with ``ts: 1`` one set member and DELETES the
      second as a duplicate.
    * ``1 == 1.0`` and they hash equal too, so tagging with
      ``type(ts).__name__`` splits a row written as an integer here and a float
      there -- an ordinary serializer artefact -- into two records and PERSISTS a
      duplicate.

    An earlier revision hit each of those in turn. One tag covers both: integers
    and floats share ``"num"`` so they still deduplicate exactly as the
    predecessor's bare-value key did, while ``bool`` is its own tag. ``bool`` is
    tested first because it is a SUBCLASS of ``int``, so an ``isinstance(ts,
    int)`` check would swallow it.

    A record with no usable ``ts`` falls back to its RAW BYTES, and a record that
    does not PARSE gets no key at all. The split is the fix. The predecessor fell
    back to ``line.strip()`` for both, and stripping is what deleted bytes: it
    makes two DISTINCT byte sequences share a key. Unstripped bytes cannot --
    byte-equal records ARE the same record, so collapsing them loses nothing.
    Withholding a key from an unparseable record covers the remaining case,
    because the fragments a split record produces are exactly the unparseable
    ones.

    That is reachable, not theoretical, and it is why the two cases are separated.
    A crash mid-append leaves a truncated row in the live file -- say ``b'{"a":
    "x'``. A source record holding a bare carriage return is split at it, because
    this reader's boundaries are the universal-newline set, yielding ``b'{"a":
    "x\r'``. Those two are NOT byte-equal, but they STRIP to the same thing, so
    the predecessor skipped the fragment as a duplicate, appended only the tail,
    and left the live file with a line parsing as neither while the source's bytes
    were gone. Measured on real bytes, before and after: stripping loses them,
    raw bytes do not. The fragment is also unparseable, so it takes the ``None``
    path and is doubly protected.

    So a ``ts``-less row that parses IS deduplicated, on bytes -- which keeps the
    predecessor's idempotence for a re-run without keeping the deletion. Only an
    unparseable record is appended unconditionally.
    Duplicating a row is recoverable; deleting one is not. ``_merge_notifications``
    validates the whole source before appending anything so that a FAILED merge
    does not leave a prefix for a retry to duplicate.

    The kind tag also keeps the two families apart: ``json.loads`` can only
    produce ``dict``, ``list``, ``str``, ``int``, ``float``, ``bool`` or ``None``,
    so ``type(ts).__name__`` is never ``"raw"`` and a byte key can never collide
    with a ``ts`` key.

    Raises :class:`UndecodableRecord` for a record that is not valid UTF-8,
    which is how the encoding property is enforced: this decode VALIDATES and
    the result is used only for the key, while what gets appended is always the
    original bytes. Validating by decoding and then writing the decoded form
    back is what makes the copy non-byte-exact in the first place.
    """
    try:
        text = record.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UndecodableRecord(f"record is not valid UTF-8 in {path!r}") from exc
    try:
        parsed = json.loads(text)
    except ValueError:
        # A record that does not PARSE keeps no key, so it is never skipped.
        # Deliberately not byte-keyed: framing splits a record at a bare
        # carriage return, and the fragments of a split record are exactly the
        # unparseable ones. Leaving them unkeyed is what makes "a fragment is
        # never mistaken for a record already present" structural rather than a
        # property of whichever collision one happens to think of.
        return None
    ts = parsed.get("ts") if isinstance(parsed, dict) else None
    # Truthiness reproduces the predecessor's `or` fallback: an absent, empty or
    # zero ts fell through to the line itself, and now falls through to None.
    if ts:
        try:
            hash(ts)
        except TypeError:
            pass
        else:
            # `bool` first: it is a subclass of `int`, so the numeric arm would
            # otherwise swallow it and re-create the True/1 collision.
            if isinstance(ts, bool):
                kind = "bool"
            elif isinstance(ts, (int, float)):
                kind = "num"
            else:
                kind = type(ts).__name__
            return (kind, ts)
    # No usable ``ts``, but the record PARSED -- so it is a whole record a
    # producer wrote, not a framing fragment, and its raw bytes are an identity
    # it is safe to deduplicate on. RAW, and never stripped: the predecessor's
    # ``line.strip()`` is what deleted bytes, because stripping makes two
    # DISTINCT byte sequences share a key -- a fragment ending in a carriage
    # return strips to a crash-truncated row that does not contain one. Keying on
    # the unstripped bytes cannot do that: byte-equal records ARE the same
    # record, so a collapse here loses no information, while a stripped key
    # collapses records that differ. That distinction is the whole fix.
    #
    # The key is the bytes that LAND, not the bytes that arrive. The append below
    # terminates an unterminated record, so keying the arriving form makes a
    # re-import compare an unterminated source row against the terminated row it
    # itself wrote, miss, and append a second copy. Normalising here uses the
    # SAME predicate as that write, so the two cannot drift.
    #
    # The direction matters and only one of the two is safe. Normalising by
    # ADDING the terminator the writer adds is deterministic and merges only
    # records that land identically. Normalising by REMOVING terminators would be
    # ``rstrip``, and that is the predecessor's deleter wearing a different name:
    # it maps ``X\r`` and ``X`` onto one key, which is precisely the fragment and
    # crash-truncated-row pair above.
    return ("raw", record if record.endswith(_TERMINATORS) else record + b"\n")


def _merge_notifications(src_path: Path, dst_path: Path) -> None:
    """Append the snapshot's notification records to the live file, byte for byte.

    This is the only merge that COPIES records rather than consuming them, so
    reading faithfully is not the whole contract -- the bytes must also be valid
    for the destination. Both handles are BINARY and framing comes from
    :func:`strict_raw_records`, because the text-mode predecessor was a locale
    decode followed by a locale encode and neither half was byte-exact:

    * Universal-newline translation on read, ``os.linesep`` on write. A record
      terminated ``\\r\\n`` was appended as ``\\n`` -- a byte silently dropped --
      and a bare ``\\r`` INSIDE a record split it in two, so both halves failed
      ``json.loads``, the ``except`` swallowed both, and the record was lost
      while the function printed success. Both fire on a pure UTF-8 host with
      fully valid UTF-8 input, so an explicit ``encoding=`` does not address
      them; ``newline=`` is a separate axis and binary mode closes both at once.
    * The locale codec decided whether an invalid-UTF-8 record aborted the
      restore or was delivered into the live file. ``for line in f`` decodes
      OUTSIDE the ``try``, so ``UnicodeDecodeError`` -- a ``ValueError`` --
      escaped the ``except (ValueError, TypeError)`` because the iterator raised
      it, not ``json.loads``. On a UTF-8 host that aborted the restore with a
      traceback; under a single-byte locale the decode succeeded, the encode put
      the same bytes back, and the live file stopped being valid UTF-8 -- after
      which its loader returns NO rows for the whole file and the next rewrite
      persists that empty view.

    The posture is ABORT, never skip, because the output feeds a durable write
    and a skipped record is a deleted one. Abort means RAISE, not warn: this
    function's callers report an outcome to somebody. ``apply_import_zip``
    appends ``notifications (merged)`` to its summary and the dashboard handler
    answers ``ok: True`` with a SEL ``outcome="ok"``, and a printed warning is
    invisible to both -- so warn-and-return would tell an API caller the import
    succeeded while records were left behind. The print stays so a CLI operator
    reads the reason before the traceback. This is why it differs from
    ``_merge_crons``, which warns and returns: a refused cron merge writes
    nothing and skips one component, while this one may already have appended a
    prefix, and the caller has to learn the write is incomplete.

    * A destination-scan failure is still a true no-op -- the destination is not
      even opened for append until that scan has completed.
    * The SOURCE is validated whole before the destination is opened for append,
      so a source-side refusal is also a no-op. Without that pass, a source whose
      Nth record is undecodable had already appended N-1 records when it aborted,
      and a retry re-appended every identity-less one of them, since a row with
      no ``ts`` cannot be deduplicated. The cost is reading the source twice.
    * A failure DURING the copy is therefore the residual case -- the source
      changed between the two passes -- and its prefix stays. Rolling it back
      would be a second unvalidated write to the live file.

    Every appended record ends with a terminator, and an unterminated final
    record already in the destination gains one before anything is appended
    after it. Without that, two records glued into one line that parses as
    neither.
    """
    existing: set[tuple[Any, ...]] = set()
    dst_unterminated = False
    try:
        with open(dst_path, "rb") as f:
            for record in strict_raw_records(f, dst_path, cap=_NOTIFICATION_RECORD_CAP):
                key = _notification_key(record, dst_path)
                if key is not None:
                    existing.add(key)
                dst_unterminated = not record.endswith(_TERMINATORS)
    except (OSError, UnreadableRecord) as exc:
        # No `_safe_name` here, deliberately: this path is the LIVE data home,
        # chosen by the operator, not a name that came out of an archive -- which
        # is the scope `_safe_name`'s own docstring states. The SOURCE prints do
        # wrap it; see the one below.
        print(f"  ⚠️  Could not read {dst_path}: {exc} — merge aborted")
        raise
    # The ENTIRE source is validated before the destination is opened for append.
    # Without this pass, a source whose Nth record is undecodable or over-cap has
    # already appended N-1 records by the time it aborts -- and a retry
    # re-appends every identity-less one of those, because a row with no ``ts``
    # cannot be deduplicated by construction. Validating first makes the source
    # side all-or-nothing in the ordinary case, so there is no prefix to
    # duplicate.
    try:
        with open(src_path, "rb") as f:
            for record in strict_raw_records(f, src_path, cap=_NOTIFICATION_RECORD_CAP):
                _notification_key(record, src_path)
    except (OSError, UnreadableRecord) as exc:
        # The PATH goes through `_safe_name` because a bundle chooses its own inner
        # root, so an archive-derived path can carry ANSI controls -- and printing
        # one raw lets a hostile archive move the cursor and overwrite lines right
        # above the prompt where the operator decides whether to trust the restore.
        #
        # The EXCEPTION deliberately does NOT, and the invariant is worth stating
        # because it is what makes the wrapper unnecessary rather than forgotten:
        # both types this arm catches already render an embedded path with
        # repr-style escaping -- `OSError.__str__` does it for its filename, and
        # `jsonl_util` uses `{path!r}` for the reason its own comment gives.
        # Measured: a control character in a directory name reaches neither
        # exception's `str()` raw. Widening this `except` tuple means re-checking
        # that, because a type formatting a path with `str()` would need the wrapper.
        print(f"  ⚠️  Could not read {_safe_name(src_path)}: {exc} — merge aborted")
        raise
    imported = 0
    try:
        with open(dst_path, "ab") as out, open(src_path, "rb") as f:
            if dst_unterminated:
                out.write(b"\n")
            for record in strict_raw_records(f, src_path, cap=_NOTIFICATION_RECORD_CAP):
                key = _notification_key(record, src_path)
                # A `None` key means the record did not PARSE, so it may be a
                # framing fragment rather than a record -- nothing it could be a
                # duplicate OF. Both `existing.add` sites refuse `None`, which is
                # what keeps it out of this membership test; an unconditional
                # `key is not None` here would be dead, and a mutation proved it
                # unobservable. A ts-less row that DOES parse is keyed on its raw
                # bytes and deduplicates normally; see _notification_key for why
                # raw and not stripped.
                if key in existing:
                    continue
                out.write(record if record.endswith(_TERMINATORS) else record + b"\n")
                if key is not None:
                    existing.add(key)
                imported += 1
    except (OSError, UnreadableRecord) as exc:
        # Reached only when the source changed BETWEEN the validation pass and
        # this one, so the prefix already appended stays: rolling it back would
        # be a second unvalidated write. Names the count so an operator knows a
        # prefix landed, and identity-less rows in it will re-append on a retry.
        print(f"  ⚠️  Stopped merging {_safe_name(src_path)} after {imported} record(s): {exc}")
        raise
    print(f"  Notifications imported: {imported}")


def _backup_and_copy(
    mc: Path,
    backup: Path,
    snap: Path,
    component: str,
    *,
    allow_unpinned: bool = False,
    installed: set[str] | None = None,
) -> None:
    """Move the live core files aside, then restore the archive's, destination pinned.

    *installed*, when given, is the rollback ledger, and this function is the ONLY place
    that may add a core file to it: a name goes in immediately before that file's own first
    mutation and never before, because the recovery leg reads membership as "this run
    reached this path". The caller used to add every file the component DECLARES up front,
    which is a different set -- a bundle may legitimately carry only some of them, and the
    loops below SKIP the rest. Recovery then saw a file with no saved copy that was
    nonetheless "installed", concluded the restore had created it, and deleted the
    operator's untouched file. Recording per file is what makes membership mean what the
    recovery leg already documents it to mean.

    The restore side used to compose ``mc / f`` and hand it to ``shutil.copy2``, so
    the destination was reached by name every time. Two consequences, both real: a
    component of the data home swapped for a link redirected the write out of the
    data home entirely, and a symlink left at the core file's own name was written
    THROUGH rather than refused -- the name-based ``islink`` check above skipped the
    backup move and then the copy followed the link it had just declined to move.

    Now the data home is pinned once and each file is created relative to that
    descriptor with ``O_EXCL``. A name that is still occupied after the backup move
    is refused instead of written through, which is the symlink case above.
    """
    if not _staging_is_pinned(allow_unpinned=allow_unpinned, what=f"restore of {component!r}"):
        for f in CORE_FILES.get(component, ()):
            # Validated before the live file is touched, and a symlink at the live name is
            # MOVED aside rather than skipped -- the same two properties the pinned branch
            # got earlier in this change. Review found this branch still carrying the old
            # behaviour: it skipped both the backup AND the replacement, so the archive's
            # file was never applied and the command reported success anyway. Moving a
            # symlink moves the link, never its target.
            if not (snap / f).is_file() or pinned_fs.is_reparse_point(snap / f):
                if (snap / f).exists():
                    print(f"⚠️  Skipping symlinked file from snapshot: {snap / f}")
                continue
            # Past the skip, so this file WILL be mutated. Recorded now, before the move
            # below, so a crash mid-write still leaves the name known to have been reached
            # -- and, just as importantly, a file skipped above is never recorded at all.
            if installed is not None:
                installed.add(f)
            live = mc / f
            if live.is_symlink() or pinned_fs.is_reparse_point(live):
                print(f"⚠️  Moving symlinked core file aside during backup: {live}")
                shutil.move(str(live), str(backup / f))
            elif live.is_file():
                shutil.move(str(live), str(backup / f))
            # Not `copy2`: it opens the destination by name for writing and follows a
            # symlink planted there in the window after the live file was moved aside,
            # overwriting whatever it points at. Review's finding. copy_file_pinned uses
            # O_CREAT|O_EXCL|O_NOFOLLOW even without a directory descriptor, so a link at
            # the destination name is refused rather than written through.
            # `fatal_skip_reporter`, NOT `_report_skip`: the live file has ALREADY been
            # moved into the backup by this point, so a skip here is not an omission from
            # an archive -- it is the live file gone AND the archive's version never
            # applied, reported as success. That is the whole reason this change has a
            # fatal reporter, and this is the third site to need it: a skip is correct
            # while PRODUCING an archive and is data loss on any path that has already
            # moved or deleted the original. Review caught this site still holding the
            # non-fatal one.
            # A collision here means something recreated the name after the live file was
            # moved aside. It is a real condition, not a skip, but it must not surface as a
            # traceback: review found the same escape on the pinned tree walk, and this
            # path had it too. The live bytes are recoverable from the backup, which is what
            # the message has to say.
            try:
                pinned_fs.copy_file_pinned(
                    str(snap / f),
                    str(mc / f),
                    on_skip=pinned_fs.fatal_skip_reporter(f"restore of {f!r}"),
                )
            except FileExistsError as exc:
                raise pinned_fs.PinnedPathRefusal(
                    f"refusing to restore {f!r}: its name was recreated while the restore "
                    "was running, so writing it would overwrite that file. The previous "
                    f"version is in {backup}. Re-run with the gateway stopped."
                ) from exc
            _lock_down_restored(mc / f, component)
        return

    src_fd = pinned_fs.open_dir_pinned(snap, what=f"snapshot payload for {component!r}")
    try:
        dst_fd = pinned_fs.open_dir_pinned(mc, what=f"data home for {component!r}")
        try:
            backup_fd = pinned_fs.create_and_open_dir_pinned(
                backup, what=f"pre-restore backup for {component!r}"
            )
            try:
                for f in CORE_FILES.get(component, ()):
                    live = mc / f
                    # Checked BEFORE the live file is touched. The archive is untrusted
                    # input, so a member that is not a regular file -- a FIFO, a device
                    # node, a directory at a core filename -- is a real possibility, and
                    # the old order moved the live file aside first and only then found
                    # the source unusable: the original ended up in the backup and
                    # nothing was restored, reported as success. Raised in review; the
                    # same validate-before-mutate ordering the platform gate follows.
                    # Asked through src_fd, not by composing a path. The by-name form
                    # re-resolved the snapshot root, so a root swapped after pinning had
                    # this guard inspecting the replacement while the copy below acted on
                    # the descriptor -- review's finding, and the same class as the
                    # destination-ownership check.
                    if not pinned_fs.is_regular_at(src_fd, f):
                        continue
                    # Same point as the fallback branch: past the skip, so this file is
                    # about to be mutated and is recorded BEFORE the move. A file the
                    # bundle does not carry never reaches here, so recovery correctly reads
                    # it as never touched and leaves it alone.
                    if installed is not None:
                        installed.add(f)
                    # A symlink at a core file's name is MOVED aside like any other
                    # occupant, not skipped. The old code skipped the move and then let
                    # the copy write through the very link it had just declined to
                    # move; skipping the whole entry instead would be no better,
                    # because the archive's version of that file would then silently
                    # never be restored. Moving a symlink moves the LINK, never its
                    # target, so nothing outside the data home is touched.
                    #
                    # The move goes through both pinned descriptors rather than
                    # shutil.move on two composed paths: review pointed out that a
                    # by-name move re-resolves both ends, so an ancestor swapped
                    # between the check and the move would relocate something else.
                    # os.rename with src_dir_fd/dst_dir_fd cannot be redirected, and it
                    # is atomic within the data home, which a copy-then-delete is not.
                    live_st = pinned_fs.stat_at(dst_fd, f)
                    if live_st is not None and _stat.S_ISLNK(live_st.st_mode):
                        print(f"⚠️  Moving symlinked core file aside during backup: {live}")
                        os.rename(f, f, src_dir_fd=dst_fd, dst_dir_fd=backup_fd)
                    elif live_st is not None and _stat.S_ISREG(live_st.st_mode):
                        os.rename(f, f, src_dir_fd=dst_fd, dst_dir_fd=backup_fd)
                    try:
                        copied = pinned_fs.copy_file_pinned(
                            str(snap / f),
                            dir_fd=src_fd,
                            name=f,
                            dst_dir_fd=dst_fd,
                            dst_name=f,
                            # Owner-only applied through the destination DESCRIPTOR, in
                            # the same call that wrote the bytes. Two things wrong with
                            # the previous _lock_down_restored(mc / f) here, both raised
                            # in review: it reopened the freshly written file BY NAME, so
                            # a link swapped in at that instant had restrict_to_owner
                            # change the permissions of whatever it pointed at; and the
                            # mode cannot be inherited from the archive, which is
                            # untrusted input -- a hand-built tarball can record 0o777 on
                            # telemetry_salt. The reviewer's suggested fix was to drop
                            # the lockdown because "the copy already applies mode", which
                            # would have done exactly that: applied the ARCHIVE's mode.
                            force_mode=0o600 if component == "security" else None,
                            # The live file was moved aside two lines up, so a skip here
                            # finishes with the original gone AND the archive's version
                            # never written. Review's third instance of that rule; it is
                            # now the reporter's job rather than a per-site check.
                            on_skip=pinned_fs.fatal_skip_reporter(f"restore of {f!r}"),
                        )
                    except FileExistsError as exc:
                        raise pinned_fs.PinnedPathRefusal(
                            f"refusing to restore {f!r}: something still occupies that "
                            "name in the data home after the backup pass, so it is a "
                            "hardlink alias or a name this restore could not move "
                            "aside. Writing to it could follow whatever it points at. "
                            "Remove it and re-run."
                        ) from exc
                    if copied and component == "security":
                        # Nothing to re-apply: force_mode above already set owner-only
                        # through the descriptor. On Windows the by-name branch still
                        # needs restrict_to_owner for its DACL, which is why that call
                        # survives there and not here.
                        pass
            finally:
                os.close(backup_fd)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def _copy_locked(src: Path, dst: Path) -> bool:
    """Copy *src* onto a missing *dst*, owner-only before the name is published.

    Merge restore only copies when *dst* is absent. Publish with ``os.link``
    (the create-only shape ``_get_telemetry_salt`` uses) so a dest that
    appears in the window — ``--force`` restore racing a live gateway
    creating ``telemetry_salt`` — raises ``FileExistsError`` and the live
    file is left alone. ``os.replace`` / ``atomic_write`` would clobber it.
    The temp is locked down before any payload is written. Failures to
    publish (unsupported hard links, a restrict error, a size mismatch)
    skip this file without raising, because merge restore has already
    applied earlier components and ``_get_telemetry_salt`` regenerates a
    missing salt. Return True only when this call published *dst*.
    """
    if dst.exists():
        return False
    if src.name == "telemetry_salt" or dst.name == "telemetry_salt":
        size = src.stat().st_size
        if size != _TELEMETRY_SALT_BYTES:
            return False
    payload = src.read_bytes()
    if dst.exists():
        return False
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    tmp_path = Path(tmp)
    try:
        platform_compat.restrict_to_owner(str(tmp_path))
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(f"short write restoring {src.name}")
            view = view[written:]
        # Drop the fd from finally before close: a close-time writeback
        # error must not be retried on an already-released descriptor,
        # because a second OSError in finally would replace the skip and
        # abort merge after earlier components were applied.
        pending = fd
        fd = -1
        os.close(pending)
        os.link(str(tmp_path), str(dst))
        return True
    except OSError:
        # FileExistsError: live dest won. EXDEV / EPERM / no-hardlink /
        # restrict / short write / close: dest stays missing and
        # `_get_telemetry_salt` regenerates. Raising here aborts merge
        # after earlier components were already applied.
        return False
    finally:
        if fd >= 0:
            pending = fd
            fd = -1
            try:
                os.close(pending)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _lock_down_restored(path: Path, component: str) -> None:
    """Apply the owner-only lockdown a restored security file needs.

    restrict_to_owner (fail-loud), NOT chmod_safe (which swallows OSError): security
    files include sel_hmac.key. Mirrors the create path's deliberate fail-loud
    lockdown -- better to abort than silently land a restored secret group- or
    world-readable. POSIX applies chmod 0o600; Windows applies an owner-only DACL
    in-process. The freshly copied file is unlinked on failure so the abort this promises
    actually removes the exposed artifact, instead of leaving the restored secret
    under the destination's inherited DACL after the OSError propagates.
    """
    if component != "security":
        return
    try:
        platform_compat.restrict_to_owner(str(path))
    except OSError:
        path.unlink(missing_ok=True)
        raise


def _backup_tree_or_refuse(src: Path, dst: Path, *, allow_unpinned: bool = False) -> None:
    """Back a live tree up, and refuse the replace if the backup is not complete.

    Replace mode later runs ``rmtree`` on the live tree, so a file the backup pass
    SKIPPED is a file the restore is about to delete with no copy anywhere -- and the
    call site is deliberately hoisted ahead of every live mutation, so a refusal
    arrives while nothing has been swapped yet (#2844). The staging
    walk legitimately skips a hardlink alias, a symlink and a non-regular file -- which
    is right when producing an archive and catastrophic here, because the skip is
    followed by a delete rather than by an omission.

    This was the second of the two findings that closed the earlier attempt at this
    change (#2446): a concurrent writer's hardlinks were skipped at backup time and then
    ``rmtree`` removed the only copies. Raised again in review against this diff, which
    had carried the same shape forward. Refusing before the delete is the only ordering
    that cannot lose data: the operator keeps a complete tree and a message naming what
    could not be copied.
    """
    _copytree_safe(
        src,
        dst,
        allow_unpinned=allow_unpinned,
        on_skip=pinned_fs.fatal_skip_reporter(f"backup of {src.name!r} before replacing it"),
    )


def _refuse_unsafe_destination_roots(mc: Path, components: list[str] | None) -> None:
    """Refuse before touching anything if a selected component's tree root is unsafe.

    Hoisted ahead of every mutation on purpose. Checking inside the per-tree loops was
    too late in the worst way: `_backup_and_copy` has already swapped the databases by
    then, so skipping an unsafe markdown tree left memory split between two versions —
    and the command still reported success. A partial restore reported as complete is
    the same lie as a partial backup reported as complete.

    Both restore modes call this. Merge is additive and destroys nothing, but a merge
    that silently omits a tree is still a merge that claims to have imported it.
    """
    offenders = []
    for comp in COMPONENTS:
        if not _want(components, comp):
            continue
        for tree in COMPONENTS[comp].trees:
            d = mc / tree
            if safe_tree_root(d, what="destination root", home=mc) is None:
                offenders.append(f"{comp}:{tree}")
    if offenders:
        raise UnsafeComponentRoot(
            "these destination trees do not resolve inside the data home: "
            + ", ".join(offenders)
            + ". Nothing has been changed. Inspect those paths (usually a symlink) "
            "and re-run — restoring past them would leave memory split between the "
            "old and new versions while reporting success."
        )


def _refuse_corrupt_source_databases(
    snap: Path,
    components: list[str] | None,
    *,
    mc_for_merge: Path | None,
) -> None:
    """Refuse a bundle whose incoming components are unsound, BEFORE any live state moves.

    Validation has to precede mutation, and for this path that is not a stylistic
    preference. Putting the incoming file where the live one was and only then checking it
    can report that the home is now sitting on a corrupt database, which is the outcome the
    check exists to prevent. A bundle arriving over the network from object storage is
    untrusted input no matter whose bucket held it, so it is validated at the point where
    refusing is still free.

    **The condition is "does this restore read or install the file", not "is this replace
    mode".** Replace installs everything it carries, so *mc_for_merge* is ``None`` and
    every declared entry is checked. Merge is per-file, because merge is not one behaviour:
    it installs some files, parses others in place, and leaves the rest alone — see
    `_merge_reads` for the three cases and why a single destination-existence test was the
    wrong proxy for all of them.

    Every incoming database for the SELECTED components is checked, not just the largest
    or the first.

    Unreadable counts as unsound. Tolerating a file named `.db` that SQLite cannot open
    is right when *creating* a snapshot (the operator's home is the source of truth and
    the file is copied verbatim), and wrong when consuming one: there the file is about
    to BECOME the operator's memory.
    """

    def _merge_reads(rel: str) -> bool:
        """Whether MERGE reads or installs *rel*, so validation has to cover it.

        A single "is the destination missing" test was a proxy, and it was wrong in two
        places, both of which merge genuinely consumes:

        * `crons.json` is PARSED when a local one exists (`_merge_crons` json-loads both
          sides) and copied when it does not. Either way merge reads it, so a malformed
          file is never harmless — skipping it because the destination exists is what let
          an unparseable file reach an unguarded `json.loads`.
        * `memory_index.db` is copied alongside `memory.db` exactly when the live
          `memory.db` is ABSENT, whatever the index's own destination looks like. Keying on
          the index's own path let a corrupt index overwrite a healthy one.

        Everything else is installed only where its own destination is missing.
        """
        assert mc_for_merge is not None
        if rel == "crons.json":
            return True
        if rel == "memory_index.db":
            return not (mc_for_merge / "memory.db").exists()
        return not (mc_for_merge / rel).exists()

    def _will_install(rel: str) -> bool:
        if mc_for_merge is None:
            return True  # replace installs everything the bundle carries
        return _merge_reads(rel)

    for component, files in CORE_FILES.items():
        if not _want(components, component):
            continue
        for name in files:
            src = snap / name
            if not src.exists() and not platform_compat.is_link_or_junction(src):
                continue  # absent from a selective bundle; nothing to validate
            if not _will_install(name):
                continue
            # "Not a file" is NOT the same as "not there". A directory (or a symlink)
            # occupying a declared file's name would otherwise read as absent, skip every
            # check below, and then let replace move the operator's live copy aside and
            # report success having restored nothing in its place.
            if not src.is_file() or platform_compat.is_link_or_junction(src):
                raise SourceComponentUnsound(
                    f"{name} in this snapshot is not a regular file.\n"
                    "   Refusing to restore: a declared component file that is a "
                    "directory or a link cannot replace the live one."
                )
            if name.endswith((".db", ".sqlite3")):
                _refuse_unless_sound(src, name, strict=True)
            elif name in COMPONENT_JSON_OBJECTS:
                # "Will this file reach a consumer?" -- not "is this a replace?". Merge
                # installs after all when the destination is ABSENT: the per-component
                # branch merges only `if dst.is_file()`, and its sibling `else` copies the
                # bundle's file in verbatim with no validation. An absent destination is the
                # FRESH MACHINE case, which is the scenario a backup exists for, so the one
                # path that skipped this check was the likeliest one to need it.
                #
                # Reproduced: a well-formed JSON ARRAY in the bundle, no live `crons.json`,
                # merge -> copied verbatim, rc=0, reported success -- and the cron loader's
                # `isinstance(data, dict) else []` branch then reports zero jobs. Every
                # schedule silently gone, nothing raised, nothing retried.
                will_install = mc_for_merge is None or not (mc_for_merge / name).is_file()
                _refuse_unless_json_object(src, name, installed=will_install)

    # Component TREES carry databases too, and a tree is copied wholesale: the knowledge
    # store lives at `workspace/knowledge/knowledge.db`, inside a tree the memory
    # component declares. Checking only the top-level declared files leaves exactly the
    # same hole one directory down.
    #
    # Strictness is per PATH, not per location. A database this product owns is strict
    # wherever it lives: `workspace/knowledge/knowledge.db` is as much ours as
    # `memory.db`, so an unopenable one is a broken bundle, not an operator's stray file.
    # Leniency exists only for the INCIDENTAL contents of a tree, where a `.db` that is
    # not SQLite is ordinary — a Windows `Thumbs.db` is on this product's own ignore list
    # — and refusing those would block restores over files that were never databases.
    for component, trees in COMPONENT_TREES.items():
        if not _want(components, component):
            continue
        for tree in trees:
            root = snap / tree
            if not root.exists() and not platform_compat.is_link_or_junction(root):
                continue  # absent from a selective bundle; nothing to validate
            if not root.is_dir() or platform_compat.is_link_or_junction(root):
                raise SourceComponentUnsound(
                    f"{tree} in this snapshot is not a directory.\n"
                    "   Refusing to restore: a declared component tree that is a file "
                    "or a link cannot replace the live one."
                )
            for src in sorted(root.rglob("*")):
                # Sidecars (`.db-wal`, `.db-shm`) do not match these suffixes, so they
                # need no separate exclusion.
                if not src.is_file() or not src.name.endswith((".db", ".sqlite3")):
                    continue
                rel = src.relative_to(snap).as_posix()
                if not _will_install(rel):
                    continue
                _refuse_unless_sound(src, rel, strict=rel in PRODUCT_TREE_DATABASES)


def _report_unmerged_databases(src_tree: Path, dst_tree: Path, tree: str) -> None:
    """Say when merge is about to KEEP a product database rather than merge it.

    Merge copies trees without overwriting, which is right for markdown: a local file that
    is newer than the bundle's must survive. Applied to one of our own databases it means
    the incoming rows are silently dropped — the operator asked to merge their knowledge
    library and got a success message that imported none of it.

    Merging those rows for real is not a copy. `knowledge.db` carries an FTS5 index plus
    foreign keys spanning `sources`, `items`, `mentions` and `source_locations`, so a
    correct merge has to remap keys, rebuild the derived index, and first decide what makes
    two documents the same document. `_merge_memory` is a hand-built per-table merge for
    exactly that reason, and there is no equivalent here yet.

    Until there is, the honest thing is to name it. Silence is what turns a known
    limitation into apparent data loss.
    """
    for rel in sorted(PRODUCT_TREE_DATABASES):
        prefix = f"{tree}/"
        if not rel.startswith(prefix):
            continue
        leaf = rel[len(prefix) :]
        if (src_tree / leaf).is_file() and (dst_tree / leaf).is_file():
            print(
                f"  ⚠️  {rel}: kept the existing database; the bundle's copy was NOT "
                "merged into it.\n"
                "      Merge mode does not combine this database's rows. To take the "
                "bundle's copy instead, use --mode replace."
            )


def _refuse_unless_json_object(src: Path, label: str, *, installed: bool) -> None:
    """Raise unless *src* parses as a JSON object.

    A database is not the only thing a restore can install broken. The consumers of these
    files treat an unreadable one as an EMPTY one — `crons.json`'s loader falls back to
    "no jobs" on both a parse error and a well-formed array — so installing a corrupt file
    silently discards the operator's content while the restore reports success. Silent
    emptiness is the worst failure available here: nothing raises, so nothing is retried.

    *installed* is what separates the two hazards, because only one of them is silent.
    Replace INSTALLS this file, so an unparseable one reaches the consumer and reads as
    empty — refusing is the only way the operator hears about it. Merge USUALLY does not
    install it: when a live copy exists, the per-component merger reads the bundle's copy,
    reports a file it cannot parse, and returns without writing live state, so the content
    is neither lost nor lost quietly. Refusing there would turn one unreadable component
    into a failed restore of every other one, which is the opposite of what an off-host
    backup is for.

    "Usually" is load-bearing, and this docstring used to say "never". Merge's per-component
    branch merges only `if dst.is_file()`; its sibling `else` copies the bundle's file in
    VERBATIM. So an absent destination -- the fresh-machine case, which is the scenario a
    backup exists for -- does install, and was the one path skipping this check. The caller
    therefore decides *installed* from "will this reach a consumer", not from "is this a
    replace". Reproduced before the fix: a well-formed JSON array, no live `crons.json`,
    merge copied it verbatim and reported success, and the cron loader then read zero jobs.

    Only structure is checked, not schema. Parsing proves the file survived transport and
    is the shape its consumer branches on; asserting field-level schema here would
    duplicate each consumer's own validation and refuse bundles those consumers accept.

    The shape checks below are gated on *installed* too, matching the parse branch,
    because the merger itself now guards the merge path: `_merge_crons` runs
    `_usable_cron_shape` over BOTH sides and returns without writing when either is
    misshapen, and that guard is a superset of this one -- it also rejects a non-string
    job name and a lone surrogate inside one.
    """
    try:
        parsed = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        if not installed:
            return
        raise SourceComponentUnsound(
            f"{label} in this snapshot could not be read as JSON ({e}).\n"
            "   Refusing to restore it over live state: its reader treats an unreadable "
            "file as an empty one, so this would discard content silently."
        ) from e
    # Everything past here is an INSTALL-path check, in ONE place rather than a gate per
    # branch, so a check added later cannot forget to carry the condition.
    #
    # The merge path is guarded by the merger: `_merge_crons` runs `_usable_cron_shape`
    # over BOTH sides and returns without writing when either is misshapen, and that guard
    # is a superset of these -- it also rejects a non-string job name and a lone surrogate
    # inside one. An earlier revision ran the shape checks in both modes on the grounds
    # that a merger's guard caught only a file it could not PARSE, so `{"jobs": ["x"]}`
    # would flow past it; that is no longer true, so refusing on the merge path would only
    # turn one misshapen component into a failed restore of every other one.
    #
    # The install path has no such guard -- it copies the file in and the consumer reads an
    # unusable one as empty -- so here this refusal is the only thing between a misshapen
    # bundle and silently discarded jobs.
    if not installed:
        return
    if not isinstance(parsed, dict):
        raise SourceComponentUnsound(
            f"{label} in this snapshot is a JSON {type(parsed).__name__}, not an "
            "object.\n"
            "   Refusing to restore it over live state: its reader expects an object and "
            "treats anything else as empty."
        )
    # An object at the top is necessary and not sufficient: the readers iterate a named
    # list and call `.get` on each entry, so a `jobs` that is not a list of objects
    # reaches attribute access on a `str` and raises mid-restore.
    for key in _JSON_OBJECT_LISTS.get(src.name, ()):
        if key not in parsed:
            continue
        entries = parsed[key]
        if not isinstance(entries, list):
            raise SourceComponentUnsound(
                f"{label} in this snapshot has '{key}' as a JSON "
                f"{type(entries).__name__}, not a list.\n"
                "   Refusing to restore it over live state: its reader iterates that "
                "key and would fail partway through."
            )
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise SourceComponentUnsound(
                    f"{label} in this snapshot has '{key}[{i}]' as a JSON "
                    f"{type(entry).__name__}, not an object.\n"
                    "   Refusing to restore it over live state: its reader reads fields "
                    "off each entry and would fail partway through."
                )


def _refuse_unless_sound(src: Path, label: str, *, strict: bool) -> None:
    """Raise unless *src* is a sound SQLite database.

    *strict* decides what an unopenable file means: a refusal for a database this product
    declares by name, and nothing at all for a `.db` found inside an operator's own tree,
    which may legitimately not be SQLite.
    """
    # SQLite treats a ZERO-BYTE file as a valid, empty database: it opens, and
    # `integrity_check` answers `ok`. So the check below cannot see the difference between
    # a healthy database and a snapshot that captured nothing, and replace mode would
    # install empty memory over live memory and report success. Size is the only place
    # that distinction is visible, so it is read before the file is opened.
    try:
        if src.stat().st_size == 0:
            raise SourceComponentUnsound(
                f"{label} in this snapshot is EMPTY (zero bytes). SQLite opens such a "
                "file as a valid empty database, so this would replace your live data "
                "with nothing and report success.\n"
                "   Refusing to restore it. Take a fresh snapshot."
            )
    except OSError as e:
        if not strict:
            return
        raise SourceComponentUnsound(
            f"{label} in this snapshot could not be read ({e}).\n"
            "   Refusing to restore it over live state."
        )
    try:
        with closing(sqlite3.connect(str(src))) as conn:
            result = conn.execute("PRAGMA integrity_check;").fetchone()[0]
    except sqlite3.Error as e:
        if not strict:
            return  # not a database; not this code's business
        raise SourceComponentUnsound(
            f"{label} in this snapshot: integrity check failed — it cannot be "
            f"opened as a database ({e}).\n"
            "   Refusing to restore it over live state."
        ) from e
    if result != "ok":
        raise SourceComponentUnsound(
            f"{label} in this snapshot: integrity check failed ({result}).\n"
            "   Refusing to restore it over live state."
        )


def _allocate_rollback_dir(mc: Path) -> Path:
    """Create a rollback directory that is this restore's alone.

    The timestamp is second-granular, so two restores inside one second would otherwise
    share a directory. That is not a naming nicety: the tree saves below refuse to write
    into an existing destination on purpose — one rollback set holding files from two
    restores rolls back to neither generation — so a shared directory turned the second
    restore into an uncaught `FileExistsError` instead of a clean refusal.

    `mkdir` without `exist_ok` is the allocation: it is atomic, so the winner of a race
    gets the name and the loser moves to the next suffix rather than both proceeding.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(1, 64):
        name = f"pre-restore-{ts}" if attempt == 1 else f"pre-restore-{ts}-{attempt}"
        candidate = mc / name
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise SourceComponentUnsound(
        f"could not allocate a rollback directory under {mc} — "
        f"'pre-restore-{ts}' and 63 suffixed variants all exist.\n"
        "   Refusing to restore without somewhere to save the current state."
    )


def _do_replace(
    snap: Path, mc: Path, components: list[str] | None, *, allow_unpinned: bool = False
) -> None:
    """Replace the selected components, with a complete rollback set taken first.

    Two phases, and the boundary between them is the whole design. Phase one copies every
    tree this run will mutate into a fresh rollback directory and mutates nothing; phase
    two performs every mutation. A refusal in phase one therefore aborts with the data home
    untouched, and a failure in phase two can be reverted from a rollback set that is known
    to be complete -- the ordering an earlier revision got wrong by running the core-file
    swap loop first, which aborted with the new databases live and the old trees live.
    """
    # Before anything is created or copied: a destination tree root that does not resolve
    # inside the data home would have the restore write outside it.
    _refuse_unsafe_destination_roots(mc, components)
    backup = _allocate_rollback_dir(mc)
    print("🔄 Replace mode — backing up current state...")

    # `memory` names workspace/memory and workspace/knowledge. Selecting it ALONE must
    # save and replace just those subtrees; when `workspace` is also selected its own pass
    # covers them, and doing both would save the INCOMING memory over the saved original.
    mem_roots: list[tuple[str, Path]] = []
    if _want(components, "memory") and not _want(components, "workspace"):
        unsafe_now = []
        for tree in COMPONENTS["memory"].trees:
            d = mc / tree
            if safe_tree_root(d, what="destination root", home=mc) is None:
                # REFUSED, not skipped. `_refuse_unsafe_destination_roots` already cleared
                # this exact set moments ago, so a root that fails here failed AFTER that
                # check -- something moved under us mid-run. This used to `continue`, which
                # dropped the tree from `mem_roots` entirely: it was then neither saved nor
                # restored, and the run still printed "Replace complete." Reproduced by
                # letting the preflight pass and swapping `workspace` for an external link
                # immediately after -- both memory trees were silently dropped and the
                # command exited 0, so an operator restoring after losing a machine would
                # believe memory came back when only the databases had. That is the very
                # lie the hoisted preflight was written to end, surviving in the one site
                # the hoist did not convert. Safe to raise here: this runs before phase
                # one, so no live state has been mutated yet.
                unsafe_now.append(f"memory:{tree}")
            else:
                mem_roots.append((tree, d))
        if unsafe_now:
            raise UnsafeComponentRoot(
                "these destination trees stopped resolving inside the data home after the "
                "pre-flight check passed: " + ", ".join(unsafe_now) + ". Nothing has been "
                "changed. A path that was safe moments ago and is not now was replaced "
                "mid-run (usually a symlink), so restoring past it would leave memory "
                "split between the old and new versions while reporting success."
            )

    # ── Phase one: the rollback set. No live state is mutated in this block. ──
    #
    # `_backup_tree_or_refuse` reports a skipped entry as FATAL, so a tree that cannot be
    # copied whole raises here rather than being rmtree'd later with an incomplete backup.
    for tree, d in mem_roots:
        if d.is_dir():
            # `tree` is NESTED (`workspace/memory`), so the rollback destination's parent
            # does not exist in a freshly-allocated backup dir. The pinned primitive pins
            # an existing parent chain and does not create one -- callers create their own
            # tree roots -- so without this the copy fails with FileNotFoundError on the
            # intermediate component. The single-level trees below are unaffected because
            # their parent IS the backup dir.
            (backup / tree).parent.mkdir(parents=True, exist_ok=True)
            _backup_tree_or_refuse(d, backup / tree, allow_unpinned=allow_unpinned)
    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            d = mc / dirname
            if d.is_dir():
                _backup_tree_or_refuse(d, backup / dirname, allow_unpinned=allow_unpinned)
    if _want(components, "skills"):
        sk = mc / "skills"
        if sk.is_dir():
            _backup_tree_or_refuse(sk, backup / "skills", allow_unpinned=allow_unpinned)

    # Every relative path phase two can write. Recovery needs it because a target that did
    # not exist before the restore has nothing saved for it, so putting saved entries back
    # would leave that creation standing.
    targets: list[str] = []
    for comp in ("memory", "crons", "config", "notifications", "security"):
        if _want(components, comp):
            targets.extend(COMPONENTS[comp].files)
    if _want(components, "workspace"):
        targets.extend(("workspace", "plan_memory"))
    if _want(components, "skills"):
        targets.append("skills")
    targets.extend(tree for tree, _ in mem_roots)

    # Grows as phase two touches each target; recovery reads it to tell a creation from a
    # target the phase never reached.
    installed: set[str] = set()
    try:
        _do_replace_mutations(
            snap, mc, backup, components, mem_roots, installed, allow_unpinned=allow_unpinned
        )
    except BaseException as e:
        # `BaseException`, not `Exception`, and deliberately wider than the three named
        # classes this used to catch. `PinnedPathRefusal` was added here because it fires
        # MID-mutation and leaving it out left live state half replaced; `KeyboardInterrupt`
        # has exactly that property and is not an `Exception` at all, so it walked past this
        # handler entirely. Reproduced: a Ctrl-C after the memory component was replaced left
        # `memory.db` holding the archive's copy and `crons.json` still the live one, with no
        # rollback attempted. A restore is the one operation where an interrupt must not be
        # taken at face value -- the operator's own state is mid-swap.
        #
        # The exception is always re-raised, so an interrupt still terminates the command and
        # `SystemExit` still exits; what changes is that the previous state is put back first.
        # A second interrupt DURING the rollback cannot be defended against here, and the
        # rollback directory is what answers for it.
        #
        # Phase one is deliberately outside this try: a refusal there happens before any
        # mutation, so there is nothing to roll back and the clean refusal is the answer.
        failed = _restore_everything_from_rollback(
            backup, mc, targets, installed, allow_unpinned=allow_unpinned
        )
        if failed:
            # The revert is part of the outcome, not a side effect of it. Re-raising the
            # original error alone would let the caller summarise this as "you are back
            # where you started", which is the one thing that must not be said when some
            # of the previous state now exists only in the rollback directory.
            raise RollbackIncomplete(e, failed, backup) from e
        raise

    try:
        backup.rmdir()
    except OSError:
        print(f"  Previous state saved to: {backup}/")
    print("✅ Replace complete.")


def _component_payload_absent(snap: Path, component: str) -> bool:
    """Does *snap* carry nothing this component could actually restore?

    Declared-but-hollow is the shape this answers: the manifest says a component rode, and the
    bundle holds none of its data. Replace then clears the live state for it -- memory trees
    are cleared unconditionally, and a derived index is now removed too -- with nothing to put
    back, which is a partial erasure rather than a restore.

    A DERIVED index is not payload. A bundle carrying only `memory_index.db` has no memory to
    restore, and counting it would let precisely the reproduced case through.

    Files AND trees both count, so a component whose data is a directory is not called hollow
    just because it keeps no flat file. Derived from `COMPONENTS` / `CORE_FILES`, never a
    hand-written list: a component gaining a file later must not silently start passing this
    check on the strength of a stale enumeration.
    """
    spec = COMPONENTS.get(component)
    if spec is None:
        return False  # not a component this function knows how to judge; do not refuse on it
    for rel in getattr(spec, "files", ()) or ():
        if rel in _DERIVED_INDEXES:
            continue
        if (snap / rel).exists():
            return False
    for rel in getattr(spec, "trees", ()) or ():
        if (snap / rel).exists():
            return False
    return True


def _drop_derived_indexes_absent_from_bundle(
    snap: Path, mc: Path, backup: Path, installed: set[str]
) -> None:
    """Remove a live derived index the archive does not carry, saving it first.

    Replace means the destination ends up matching the archive. The memory-TREE loop already
    says so and clears unconditionally for it; a derived index is a FILE and never got the
    same treatment, so `_backup_and_copy` skipped it (`(snap / f).is_file()` is false) and
    left the live one in place. The result is the restored payload indexed by the PREVIOUS
    one: searches answer from memory that was just replaced.

    That gap is reachable precisely because the redaction pass DROPS `memory_index.db` from an
    off-host bundle -- so a redacted bundle is the ordinary way to arrive here, not an exotic
    one. The comment justifying that drop pointed at restore "telling the operator to rebuild
    it", which is true only when the live index is absent too: the warning tests the file
    after the restore, and a surviving stale index means no warning is printed at all.

    Removing it is what makes the absence real, so the existing warning fires and the index is
    rebuilt from the restored payload. Scoped to derived indexes ON PURPOSE -- a missing
    payload database is a different question with a different answer (refuse, not delete), and
    it is answered elsewhere.
    """
    for rel in sorted(_DERIVED_INDEXES):
        if (snap / rel).is_file():
            continue  # the archive carries it; the ordinary copy path applies
        live = mc / rel
        if not (live.is_file() or live.is_symlink() or platform_compat.is_link_or_junction(live)):
            continue
        # Recorded immediately before the move, never earlier: the recovery leg reads
        # membership as "this run reached this path", and the move below IS the save it
        # needs. Adding the name before a save is known is what let recovery delete an
        # occupant it had never saved.
        installed.add(rel)
        if platform_compat.is_link_or_junction(live) and not live.is_symlink():
            # A junction is a directory reparse point; `shutil.move` on it is not the
            # pairing this repo uses. Remove the link itself -- there is nothing to save,
            # because the link's target is not ours and stays where it is.
            platform_compat.unlink_link_or_junction(live)
        else:
            shutil.move(str(live), str(backup / rel))
        print(f"  ↩️  {rel} is not in the archive — moved aside so it can be rebuilt")


def _do_replace_mutations(
    snap: Path,
    mc: Path,
    backup: Path,
    components: list[str] | None,
    mem_roots: list[tuple[str, Path]],
    installed: set[str],
    *,
    allow_unpinned: bool = False,
) -> None:
    """Every mutation replace mode performs, so one handler can revert all of them.

    *installed* accumulates every declared path this run begins writing, and is recorded
    BEFORE the write rather than after, so a target interrupted mid-write is still known
    to have been reached. Recovery needs that: a file is saved by moving it aside at the
    moment of its own mutation, so "nothing saved" is ambiguous until you know whether the
    phase ever got there.

    A memory tree is CLEARED unconditionally and refilled only when the archive carries it,
    because replace means the destination ends up matching the archive. Do not read
    `_trees_absent_from_bundle` as covering that: it refuses only bundles with no component
    map, and a v3 bundle may legitimately declare `memory` without carrying every tree of it.
    """
    for comp in ("memory", "crons", "config", "notifications", "security"):
        if _want(components, comp):
            _backup_and_copy(
                mc, backup, snap, comp, allow_unpinned=allow_unpinned, installed=installed
            )
            print(f"  ✅ {comp}")

    if _want(components, "memory"):
        # After the copy, not before: the loop above is what installs the index when the
        # archive HAS it, and this only has to answer for the case where it does not.
        _drop_derived_indexes_absent_from_bundle(snap, mc, backup, installed)

    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            d = mc / dirname
            sd = snap / dirname
            if sd.is_dir():
                installed.add(dirname)
                if d.is_dir():
                    shutil.rmtree(str(d))
                # rmtree just removed the live tree, so a skipped source entry here means
                # that file exists in neither place.
                #
                # `must_create` is what makes the removal mean something. Without it the
                # walk accepted a root recreated between the rmtree and the copy, so files
                # the archive does not contain survived a REPLACE that reported success.
                _copytree_safe(
                    sd,
                    d,
                    allow_unpinned=allow_unpinned,
                    must_create=True,
                    on_skip=pinned_fs.fatal_skip_reporter(f"restore of {dirname!r}"),
                )
        print("  ✅ workspace")

    if _want(components, "skills"):
        sk = mc / "skills"
        snap_sk = snap / "skills"
        if snap_sk.is_dir():
            installed.add("skills")
            if sk.is_dir():
                shutil.rmtree(str(sk))
            _copytree_safe(
                snap_sk,
                sk,
                allow_unpinned=allow_unpinned,
                # Same as the workspace branch above: the tree was just removed, so a root
                # that exists again was recreated by something else.
                must_create=True,
                on_skip=pinned_fs.fatal_skip_reporter("restore of 'skills'"),
            )
        print("  ✅ skills")

    # Scoped to memory's own subtrees, and skipped entirely when `workspace` is selected:
    # that pass has already replaced these paths, and repeating the work here would save
    # the INCOMING memory over the saved original.
    for tree, d in mem_roots:
        sd = snap / tree
        # CLEARED UNCONDITIONALLY, then filled only if the archive carries the tree.
        # Clearing only when the archive had it meant a bundle without, say,
        # `workspace/knowledge` left the destination's own knowledge tree in place, so a
        # "replace" produced restored memory mixed with stale notes and still reported
        # success. Replace means the destination ends up matching the archive; a tree the
        # archive does not have is a tree the destination must not keep. The rollback copy
        # was taken in phase one, before any database was swapped, so the removed state is
        # still recoverable.
        #
        # `_trees_absent_from_bundle` does NOT cover this: it only refuses bundles carrying
        # no component map, which is the escape hatch for pre-seam archives. A v3 bundle
        # declares `memory` and legitimately may not carry every tree of it, and this is
        # the branch that has to answer for that.
        if d.is_dir() or platform_compat.is_link_or_junction(d):
            installed.add(tree)
            if platform_compat.is_link_or_junction(d):
                # `unlink_link_or_junction`, not `Path.unlink`. Detecting with
                # `is_link_or_junction` and removing with plain unlink is the mispairing that
                # helper's own docstring warns against: a Windows junction is a DIRECTORY
                # reparse point, so `unlink` raises on it and the tree is never cleared --
                # the replace then fails mid-flight on a platform where this is the ordinary
                # shape of a linked tree.
                platform_compat.unlink_link_or_junction(d)
            else:
                shutil.rmtree(str(d))
        if sd.is_dir():
            installed.add(tree)
            # Nested destination, same reason as the rollback save: the pinned primitive
            # requires the parent chain to exist and creates only the final directory. A
            # home that has no `workspace/` at all is the ordinary case for a restore onto
            # a fresh machine, which is exactly what this component is for.
            d.parent.mkdir(parents=True, exist_ok=True)
            _copytree_safe(
                sd,
                d,
                allow_unpinned=allow_unpinned,
                must_create=True,
                on_skip=pinned_fs.fatal_skip_reporter(f"restore of {tree!r}"),
            )
    if mem_roots:
        print("  ✅ memory trees")


def _restore_everything_from_rollback(
    backup: Path, mc: Path, targets: list[str], installed: set[str], *, allow_unpinned: bool = False
) -> list[str]:
    """Undo the mutation phase, target by target, using *targets* as the granularity.

    The recovery half of replace-mode atomicity. Undoing the whole saved set returns the
    data home to one coherent generation regardless of how far the pass got. Recovering
    only the item that failed is what leaves memory half-old and half-new.

    **Granularity is the invariant, and it is exactly *targets*.** Every entry is a
    declared relative path, and recovery touches nothing else. Walking the rollback
    DIRECTORY instead looks equivalent and is not: memory's trees are nested
    (``workspace/memory``), so ``backup`` contains a partial ``workspace/`` holding only
    those subtrees. Treating that directory as one unit clears the live ``workspace``
    whole and puts the partial copy back — deleting unrelated workspace data the restore
    never touched. Restoring `workspace/memory` restores `workspace/memory`.

    Three cases per target, and the third is why *installed* exists:

    * **Saved** — put it back, clearing only that path.
    * **Not saved, and this run installed it** — it did not exist before, so the copy the
      restore created is REMOVED. That is what "no pre-restore state" restores to.
    * **Not saved, and this run never reached it** — LEFT ALONE. Absence of a saved copy
      does not mean absence of prior state: a file is saved by MOVING it aside at the
      moment of its own mutation, so a failure partway through the phase leaves every
      later target untouched and unsaved. Removing those deletes the operator's own data
      that this restore never so much as opened, which is the opposite of recovery.

    Best-effort per target, and it says so per target: a recovery that aborts on its
    first problem strands the rest, and by this point the operator's own data is what is
    at stake. Whatever cannot be undone is named, and this function never deletes the
    rollback directory.
    """
    if not backup.is_dir():
        print(f"⚠️  No rollback directory at {backup}; nothing to put back.")
        # Reported as a failed revert, not as success with a warning: the caller's summary
        # line is what the operator acts on, and "put back" would be false here.
        return list(sorted(set(targets)))
    print(f"↩️  Restoring the previous state from {backup} ...")
    failed: list[str] = []
    for rel in sorted(set(targets)):
        saved = backup / rel
        target = mc / rel
        try:
            if platform_compat.is_link_or_junction(saved):
                # FIRST, because both tests below DEREFERENCE. A core file that was a
                # relative symlink stops resolving the moment it is moved into the rollback
                # directory, so `is_dir()` and `is_file()` are both false and the saved link
                # matched no branch at all: the replacement at the live name was deleted as
                # an undone creation, the link stayed in the rollback directory, and the
                # recovery reported success. Reproduced -- the live name ended up not
                # existing at all.
                #
                # The directory branch below says the rollback directory is "links-free by
                # construction". That is true of TREES, which `_backup_tree_or_refuse` saves
                # with a fatal skip reporter. Core FILES are saved by `_backup_and_copy`,
                # which deliberately MOVES a symlinked core file aside and prints that it
                # did -- so a link here is a state this code creates on purpose, and the
                # claim was being applied to an input it was never about.
                target.parent.mkdir(parents=True, exist_ok=True)
                if platform_compat.is_link_or_junction(target):
                    platform_compat.unlink_link_or_junction(target)
                elif target.is_dir():
                    shutil.rmtree(str(target))
                elif target.exists():
                    target.unlink()
                # MOVED back rather than copied, unlike the two branches below. The save
                # moved the link itself, so the rollback holds the only copy; and a move is
                # the one operation that also reinstates a Windows junction, whose target
                # cannot be read portably. Moving a link moves the LINK, never its target.
                shutil.move(str(saved), str(target))
            elif saved.is_dir():
                # Clearing the live root before refilling it. A root that passed
                # containment can still BE a link -- one pointing elsewhere inside the
                # data home resolves within it -- and rmtree raises on a link, which at
                # this point would strand the recovery. Remove a link as a link and
                # reserve rmtree for real directories.
                #
                # Through `unlink_link_or_junction`, because a Windows junction is a
                # directory reparse point that `Path.unlink` cannot remove: recovery would
                # raise here and leave the operator's prior state stranded, which is the
                # one outcome this whole function exists to prevent.
                if platform_compat.is_link_or_junction(target):
                    platform_compat.unlink_link_or_junction(target)
                elif target.is_dir():
                    shutil.rmtree(str(target))
                target.parent.mkdir(parents=True, exist_ok=True)
                # The plain staging copy is lossless HERE, which it would not have been
                # for the save. `_backup_tree_or_refuse` reports a skipped entry as fatal,
                # so a TREE containing a link never reaches the rollback directory at all
                # -- whatever `backup` holds for a tree is links-free by construction, and
                # there is nothing for a link-preserving copy to preserve on the way back.
                #
                # Scoped to trees deliberately. This used to read as a claim about the whole
                # rollback directory, which is false: core FILES are saved by a different
                # function that MOVES a symlink aside on purpose, and reading the claim that
                # broadly is what left the saved-link case with no branch to match.
                _copytree_safe(
                    saved,
                    target,
                    # The operator's opt-in has to reach HERE, not just the forward path.
                    # Without it this copy took the default and refused on a platform with
                    # no directory descriptors -- so an operator who passed
                    # `--allow-unpinned-staging` got a replace that was allowed to MUTATE
                    # live state and a rollback that then refused to put it back. The safety
                    # net became the one thing that would not run, at the only moment it
                    # matters. Found by a test that could not be fixed from the test side
                    # because this function had no way to be told.
                    allow_unpinned=allow_unpinned,
                    must_create=True,
                    on_skip=pinned_fs.fatal_skip_reporter(f"rollback of {rel!r}"),
                )
            elif saved.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                # Routed through the pinned primitive, not `shutil.copy2`. copy2 opens the
                # destination BY NAME and FOLLOWS a link sitting there, so a link planted at
                # a core file's name between the save and this recovery would send the
                # restored bytes to whatever it points at. The directory branch above
                # already refuses a link; this is the same hazard in the sibling branch, and
                # recovery is the worst place to have it -- it runs precisely when the
                # operator's state is already half-replaced.
                #
                # An existing destination is REPLACED here, unlike the merge path: this is
                # putting back what the restore moved aside, so `skip_existing` would leave
                # the failed generation in place. `copy_file_pinned` opens
                # O_CREAT|O_EXCL|O_NOFOLLOW, so the old name is removed first and a link at
                # that name is refused rather than followed.
                if platform_compat.is_link_or_junction(target):
                    platform_compat.unlink_link_or_junction(target)
                elif target.is_file():
                    target.unlink()
                pinned_fs.copy_file_pinned(
                    str(saved),
                    str(target),
                    on_skip=pinned_fs.fatal_skip_reporter(f"rollback of {rel!r}"),
                )
            elif rel in installed and (
                target.exists() or platform_compat.is_link_or_junction(target)
            ):
                # Nothing saved, so the only justification for deleting is that this run
                # created it. `rel in installed` is NOT that evidence: the name is recorded
                # BEFORE the save is known to have happened (deliberately -- a crash
                # mid-write must still leave it known to have been reached), and the save
                # only actually happens on two branches, a symlink or a regular file. A core
                # file's path occupied by a DIRECTORY matches neither, so nothing is saved
                # and the name is recorded anyway.
                #
                # Reproduced: with a directory at `crons.json` holding operator data,
                # recovery deleted the directory and its contents, reported an empty failure
                # list, and printed "Previous state restored." Data loss announced as a
                # successful recovery.
                #
                # So the type is the evidence. A core file entry is one this run would have
                # created as a regular FILE; a directory standing there is something else's,
                # and deleting it is unrecoverable while leaving it is not. Recorded as a
                # failure so the operator is told rather than silently obeyed.
                if target.is_dir() and not platform_compat.is_link_or_junction(target):
                    if rel in CORE_FILES_FLAT:
                        failed.append(
                            f"{rel} (a directory is standing where this component's FILE "
                            "belongs, and no copy of it was saved -- refusing to delete it, "
                            "because nothing here proves this run created it)"
                        )
                        continue
                    shutil.rmtree(str(target))
                else:
                    # Covers a link or junction as well as a plain file, so it goes through
                    # the helper: `Path.unlink` cannot remove a Windows junction, and the
                    # helper falls through to `unlink` for an ordinary file anyway.
                    platform_compat.unlink_link_or_junction(target)
        # `PinnedPathRefusal` alongside OSError, and NOT an OSError itself: recovery now
        # restores through the pinned primitives with a fatal reporter, so one target it
        # cannot put back raises a refusal. Recorded per target like any other failure --
        # aborting the loop here would strand every remaining target, which is the opposite
        # of recovery, and by this point the data at stake is the operator's own.
        except (OSError, pinned_fs.PinnedPathRefusal) as e:
            failed.append(f"{rel} ({e})")
    if failed:
        print("⚠️  Could not undo these: " + ", ".join(failed))
        print(
            f"   The saved copies are still in {backup} — recover them by hand before "
            "re-running."
        )
    else:
        print("↩️  Previous state restored.")
    return failed


def _do_merge(
    snap: Path, mc: Path, components: list[str] | None, *, allow_unpinned: bool = False
) -> None:
    # BOTH restore modes refuse an unsafe destination tree root up front, and merge needs
    # it for the same reason replace does: a merge that silently omits a tree is still a
    # merge that claims to have imported it. Skipping the tree and returning 0 is the worst
    # available outcome -- the operator is told the import succeeded while the notes they
    # were importing are not there.
    _refuse_unsafe_destination_roots(mc, components)
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
                shutil.copy2(str(sn), str(dn))
                print("  Notifications: copied")
        print("  ✅ notifications")

    if _want(components, "security"):
        for f in CORE_FILES["security"]:
            s, d = snap / f, mc / f
            if s.is_file() and not d.is_file():
                if _copy_locked(s, d):
                    print(f"  {f}: restored (was missing)")
        print("  ✅ security")

    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            sd = snap / dirname
            if sd.is_dir():
                dd = mc / dirname
                dd.mkdir(parents=True, exist_ok=True)
                _copy_tree_no_overwrite(sd, dd, allow_unpinned=allow_unpinned)
        print("  ✅ workspace")

    if _want(components, "skills"):
        if (snap / "skills").is_dir():
            (mc / "skills").mkdir(parents=True, exist_ok=True)
            _copy_tree_no_overwrite(snap / "skills", mc / "skills", allow_unpinned=allow_unpinned)
        print("  ✅ skills")

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
                hollow = [c for c in declared if _component_payload_absent(snap, c)]
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
            print(
                "   Putting your previous state back did NOT fully succeed. Some of it "
                f"exists only in {_safe_name(str(e.backup))} now:"
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
