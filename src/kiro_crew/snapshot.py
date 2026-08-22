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
import tarfile
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

from kiro_crew import platform_compat

if TYPE_CHECKING:
    from kiro_crew import snapshot_redact
from kiro_crew.deploy import profiles as _profiles

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


def _remote() -> Any:
    """The off-host destination module, imported on first use.

    `snapshot` is on the gateway's boot path, so importing the S3 code here would put it
    there too — and a gateway never uploads anything. Resolved when a command that
    actually talks to a destination asks for it, so `kirocrew gateway` reaches readiness
    without loading it. A ratchet compares the boot module set against the base branch's.
    """
    return importlib.import_module("kiro_crew.snapshot_remote")


def _redactor() -> Any:
    """The outbound redaction pass, imported on first use. Same reason as `_remote`."""
    return importlib.import_module("kiro_crew.snapshot_redact")


def _safe_name(value: object, default: str = "unknown") -> str:
    """Render a name that came out of an ARCHIVE printable.

    Tar member names, manifest keys and archive root directories are all chosen by
    whoever wrote the bundle, and a bundle can arrive from object storage. Printing one
    raw means the terminal INTERPRETS whatever escape sequences it holds: the cursor
    moves, lines get overwritten, and a hostile archive can dress itself up as a
    different, expected one — right above the prompt where the operator decides whether
    to restore it. Two of these sites print while REJECTING a hostile entry, so the
    raw name there is precisely the attacker's payload.

    Same treatment as S3 object keys, for the same reason: the bytes are not ours.
    """
    return _remote().safe_for_terminal(str(value if value else default))


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
    """
    names = list(COMPONENTS) if requested is None else requested
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


def _restage_databases(src_dir: Path, dst_dir: Path) -> None:
    """Re-copy every SQLite database under *src_dir* through the backup API.

    The plain tree copy already placed a byte copy there; this replaces it with a
    consistent one. Done as a second pass rather than by filtering the tree walk, so
    the copy logic stays in one place and a database newly appearing in a tree is
    covered without anyone remembering to register it.

    A file whose suffix says database but which SQLite cannot open is left as the byte
    copy already made: a non-database that happens to be named ``.db`` is still the
    operator's file and must ride the bundle.
    """
    for src in sorted(src_dir.rglob("*")):
        if not src.is_file() or src.is_symlink() or src.suffix not in _DB_SUFFIXES:
            continue
        dst = dst_dir / src.relative_to(src_dir)
        if not dst.parent.is_dir():
            continue
        # `as_uri()` percent-escapes the path. Interpolating it raw meant a POSIX
        # filename containing `?` or `#` was parsed as the start of the URI's query or
        # fragment, truncating the path — so the copy would open a DIFFERENT database
        # and store it under the requested name. Built ONCE and reused by both the probe
        # and the copy: two spellings of the same URI is how one of them drifts.
        ro_uri = f"{src.resolve().as_uri()}?mode=ro"
        # Two different failures hide behind sqlite3.Error here, and treating them
        # alike is unsafe. If the file is not a database at all, the plain byte copy
        # already staged is exactly right. If it IS a database and the backup call
        # failed -- an exclusive writer, an I/O error -- the staged copy is a raw
        # snapshot of the file WITHOUT its WAL sidecars, which this module deliberately
        # excludes, so the bundle would carry a torn database and report success.
        #
        # They are told apart by probing readability separately from copying, rather
        # than by inspecting the error: `sqlite_errorname` is 3.11+ and this package
        # supports 3.10, and matching on message text would break with any SQLite
        # wording change.
        try:
            with closing(sqlite3.connect(ro_uri, uri=True)) as probe:
                probe.execute("PRAGMA schema_version").fetchone()
        except sqlite3.DatabaseError as e:
            # Only a POSITIVELY identified non-database keeps the plain copy. The broad
            # form was wrong in the dangerous direction: "database is locked" is also a
            # DatabaseError, so an exclusive writer made the probe report "not a
            # database" and the raw byte copy -- taken WITHOUT its WAL sidecars, which
            # this module excludes -- shipped as if it were consistent.
            #
            # `sqlite_errorname` is 3.11+ and this package supports 3.10, so it is read
            # defensively and the message is the documented fallback. Either way the
            # DEFAULT is to raise: an error this code cannot classify is not evidence
            # that the file is safe to copy byte-for-byte.
            name = getattr(e, "sqlite_errorname", "")
            not_a_database = name == "SQLITE_NOTADB" or (
                not name and "not a database" in str(e).lower()
            )
            if not not_a_database:
                raise DatabaseCopyFailed(src, e) from e
            print(
                f"⚠️  {_safe_name(src.name)} is not a readable SQLite database "
                "— copied as a plain file"
            )
            continue
        with (
            closing(sqlite3.connect(ro_uri, uri=True)) as src_conn,
            closing(sqlite3.connect(str(dst))) as dst_conn,
        ):
            # The file is a readable database, so a failure here means the consistent
            # copy did not happen and the staged file is a raw copy WITHOUT its WAL
            # sidecars. Absorbing that would ship a torn database, so it is raised --
            # but as a typed error naming the file, so the command boundary can report
            # which database failed instead of exiting on a traceback.
            try:
                src_conn.backup(dst_conn)
            except sqlite3.Error as e:
                raise DatabaseCopyFailed(src, e) from e


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


class RollbackNotFaithful(OSError):
    """A rollback copy cannot reproduce the live tree, so the restore must not start.

    An `OSError` so the restore's existing IO handling reports it as a refusal rather
    than letting it escape as a traceback.
    """


def _copytree_rollback(src: Path, dst: Path) -> None:
    """Copy a tree for ROLLBACK, reproducing link nodes instead of dropping them.

    `_copytree_safe` omits links, which is right for a bundle that leaves the host and
    wrong here. A rollback copy exists to put the live tree back exactly as it was, so a
    link the copy drops is a link recovery deletes: it clears the live tree and restores
    from a set that never held the node. That is the incomplete-rollback failure the
    save-before-mutation ordering exists to prevent, so the same ordering has to be paired
    with a copy that is actually faithful.

    Links are recreated, never followed, so nothing outside the tree is read or copied.
    A junction cannot be recreated portably; meeting one means this copy cannot be
    faithful, and because the save runs before any mutation, refusing here leaves the data
    home untouched.
    """
    for node in src.rglob("*"):
        if platform_compat.is_link_or_junction(str(node)) and not node.is_symlink():
            raise RollbackNotFaithful(
                f"{node} is a junction, which a rollback copy cannot reproduce. "
                "Refusing before anything is changed — move or remove it, then retry."
            )
    shutil.copytree(src, dst, symlinks=True)


def _copytree_safe(src: Path, dst: Path, **kwargs) -> None:
    """copytree that skips links to prevent sensitive file leakage.

    Uses :func:`platform_compat.is_link_or_junction`, not ``os.path.islink``, because
    ``islink`` returns False for a Windows directory junction: a junction nested inside
    a component tree would be treated as a real directory and copied THROUGH, pulling
    whatever it points at (a credential directory, say) into the bundle and then to S3.
    ``safe_tree_root`` guards the tree's ROOT; this guards every node below it, and the
    two must agree on what counts as a link or the weaker one decides.
    """
    outer_ignore = kwargs.pop("ignore", None)

    def _ignore_links(directory, contents):
        skipped = {
            name
            for name in contents
            if platform_compat.is_link_or_junction(os.path.join(directory, name))
        }
        for name in skipped:
            print(f"⚠️  Skipping link in source tree: {os.path.join(directory, name)}")
        if outer_ignore:
            skipped |= set(outer_ignore(directory, contents))
        return skipped

    shutil.copytree(str(src), str(dst), ignore=_ignore_links, **kwargs)


def _clear_tree_root(d: Path) -> None:
    """Remove a live tree root so incoming files can replace it.

    THE chokepoint for this operation, because the naive form is subtly wrong in a way
    that bites mid-restore. ``d.is_dir()`` follows a symlink, so a root that is a link to
    a directory answers True — and ``shutil.rmtree`` then refuses a symlink with OSError.
    By the time this runs, databases have already been replaced, so an exception here
    leaves the operator half-restored: the worst outcome available.

    A link is removed as a link; only a real directory is walked.
    """
    if platform_compat.is_link_or_junction(d):
        platform_compat.unlink_link_or_junction(str(d))
    elif d.is_dir():
        shutil.rmtree(str(d))


def _copy_tree_no_overwrite(src: Path, dst: Path, home: Path | None = None) -> None:
    """Merge *src* into *dst* without overwriting, refusing to write outside *home*.

    ``safe_tree_root`` validates the destination ROOT, but this function walks below it
    and the DESTINATION side can contain links too. A nested link under ``dst`` -- say
    ``workspace/memory/history`` pointing at a directory outside the data home -- would
    otherwise be followed on write, and the merge would deposit restored files wherever
    it aimed. Guarding the source alone is not enough: the write target is the dangerous
    end here.

    Each target is therefore checked by *resolved containment* against the resolved
    home, which is the same predicate ``safe_tree_root`` uses. When *home* is None the
    containment check is skipped, which is only appropriate for staging into a
    freshly-created temporary tree.
    """
    resolved_home = home.resolve() if home is not None else None

    def _inside(target: Path) -> bool:
        if resolved_home is None:
            return True
        # Climb to the nearest existing ancestor, because the target itself usually does
        # not exist yet. `exists()` alone is not enough to decide "not there": it FOLLOWS
        # links, so a BROKEN symlink answers False and the climb would step straight past
        # it — then `mkdir(parents=True)` meets the dangling link and raises
        # FileExistsError, aborting a merge that has already replaced the databases.
        # A link is therefore rejected outright, dangling or not: it is not a directory we
        # are willing to write through.
        probe = target
        while probe != probe.parent:
            if platform_compat.is_link_or_junction(probe):
                return False
            if probe.exists():
                break
            probe = probe.parent
        try:
            probe.resolve().relative_to(resolved_home)
        except (ValueError, OSError):
            return False
        return True

    for item in src.rglob("*"):
        # Same reasoning as _copytree_safe: a junction is not an islink, and this path
        # walks INTO directories, so an unguarded junction would be descended.
        if platform_compat.is_link_or_junction(item):
            continue
        target = dst / item.relative_to(src)
        if not _inside(target):
            print(f"⚠️  Skipping merge target outside the data home: {target}")
            continue
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Staged beside the target and renamed into place, because this merge SKIPS a
            # target that already exists: a copy that dies partway (a full disk is enough)
            # would otherwise leave a truncated file that every later retry treats as
            # already merged, so the corruption becomes permanent and silent. A rename is
            # atomic within the directory, so the target either is not there or is whole.
            #
            # The staging name is created EXCLUSIVELY rather than derived from the target.
            # A derived name is a path the operator may already own — and if theirs is a
            # link, writing to it follows the link and deposits the file wherever it
            # points, which is the containment this walk exists to enforce. `mkstemp`
            # cannot collide with or follow anything that already exists.
            fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".merge-")
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                shutil.copy2(str(item), str(tmp))
                # `os.link`, not `os.replace`: the existence test above is not a hint,
                # it is the promise this mode makes — merge adds what is missing and
                # never touches what is there. `os.replace` overwrites unconditionally,
                # so anything appearing in that window (the operator's own write, a
                # concurrent restore) is destroyed by a mode that guaranteed it would
                # not be. A link into the same directory is atomic like the rename AND
                # fails when the target exists, so the file is whole or absent, and
                # never someone else's work replaced.
                try:
                    os.link(str(tmp), str(target))
                except FileExistsError:
                    continue  # it arrived after the test; merge leaves it alone
                except OSError:
                    # A filesystem that will not hard-link at all (some removable
                    # formats, and Windows outside NTFS). Fall back to creating the
                    # TARGET exclusively, which still cannot clobber; only the
                    # crash-safety of the link is lost, so a failed write is unlinked
                    # here rather than left looking already-merged.
                    if not _merge_exclusive_copy(item, target):
                        continue
            finally:
                tmp.unlink(missing_ok=True)


def _merge_exclusive_copy(src: Path, target: Path) -> bool:
    """Copy *src* to *target*, creating it exclusively. False if it already existed.

    The fallback for a filesystem without hard links. `O_EXCL` keeps the no-overwrite
    promise; the cleanup keeps a half-written file from being mistaken for a merged one,
    which is what makes such a failure permanent -- every later run skips a target that
    exists.
    """
    try:
        fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        with open(fd, "wb", closefd=True) as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
        shutil.copystat(str(src), str(target))
    except OSError:
        target.unlink(missing_ok=True)
        raise
    return True


# ── Snapshot ──────────────────────────────────────────────────────────────────


class UnsafeComponentRoot(Exception):
    """A selected component's tree root does not resolve inside the data home.

    Raised rather than skipped: a bundle whose manifest claims a component it could not
    read is a backup that lies about its contents, which is worse than a refusal.
    """


class DestinationUnresolved(Exception):
    """No usable AWS profile is registered for an off-host destination."""


def _resolve_aws_profile(explicit: str | None) -> tuple[str, str]:
    """Return (profile, region) for the backup destination.

    Reuses the deploy module's profile-name registry rather than introducing a
    second place to configure AWS identity. Only names are stored there — never
    keys — and the CLI resolves the actual credentials.

    An unregistered name resolves to ``None`` there, which is a refusal, not a
    fallback: a backup must run under a profile the operator registered, so the
    caller reports it instead of silently reaching for some other identity.
    """
    resolved = _profiles.resolve_profile(explicit or "")
    if resolved is None:
        raise DestinationUnresolved(
            f"no registered AWS profile for {explicit!r}"
            if explicit
            else "no default AWS profile is registered"
        )
    return resolved


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
    bundle. `--to-s3` with no `--components` stages everything, which includes the config
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


class _RedactionFailed(Exception):
    """Redaction could not be completed, so there is nothing safe to upload."""


def _redacted_upload_copy(
    outfile: Path, workdir: Path
) -> "tuple[Path, snapshot_redact.RedactionReport] | None":
    """Build a redacted archive to upload in place of *outfile*.

    Returns ``None`` only when redaction is deliberately DISABLED by config — the one case
    where sending the original is the intended behaviour. Every failure raises
    `_RedactionFailed` instead, because "could not redact" must never fall through to
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
        raise _RedactionFailed(
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
                # archive and I/O failures -- so `--to-s3` crashed on those interpreters
                # while the local snapshot had already been written.
                members = [m for m in tf.getmembers() if _data_filter(m) is not None]
                tf.extractall(path=str(stage), members=members)  # nosec B202
    except (tarfile.TarError, OSError, EOFError, _ArchiveTooLarge) as e:
        raise _RedactionFailed(f"could not read the bundle back to redact it ({e})") from e
    roots = [d for d in stage.iterdir() if d.is_dir()] if stage.is_dir() else []
    if len(roots) != 1:
        raise _RedactionFailed(f"expected one bundle root to redact, found {len(roots)}")

    try:
        report = _redactor().redact_bundle_for_egress(roots[0])
    except _redactor().PayloadDatabaseUnprovable as e:
        shown = ", ".join(_safe_name(x) for x in sorted(e.details))
        raise _RedactionFailed(
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
        raise _RedactionFailed(
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
        raise _RedactionFailed(f"the redaction pass failed ({e})") from e
    redacted = workdir / f"{outfile.stem}.redacted.tar.gz"
    try:
        with tarfile.open(redacted, "w:gz") as tf:
            tf.add(str(roots[0]), arcname=roots[0].name)
        platform_compat.restrict_to_owner(str(redacted))
    except OSError as e:
        raise _RedactionFailed(f"could not write the redacted archive ({e})") from e
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


def _upload_bundle(outfile: Path, args: argparse.Namespace, selected: list[str]) -> int:
    """Send a written bundle to the configured off-host destination.

    Deliberately does NOT accept a bucket from the caller. Provisioning is a separate,
    explicit act (`kirocrew backup setup`) and this path writes only to what that
    recorded — which is what removes "decide whether an arbitrary bucket is safe" from
    every backup run.
    """
    _report_unresolved_payload(selected)
    try:
        dest = _remote().load_destination()
    except _remote().DestinationNotConfigured as e:
        print(f"❌ {e}")
        return 1
    except _remote().DestinationError as e:
        print(f"❌ {e}")
        return 1
    try:
        profile, _region = _resolve_aws_profile(getattr(args, "aws_profile", None))
    except (DestinationUnresolved, OSError, ValueError) as e:
        print(f"❌ Could not resolve an AWS profile: {e}")
        return 1

    # The download side refuses an archive declaring more than the bound, so uploading
    # one past it would publish a bundle that can never be restored through this tool --
    # and S3 accepts it, because the object is well under the upload limit once
    # compressed. The asymmetry is the bug: the same bound is applied here, before
    # anything is published, so the failure lands where the operator can still act on it
    # instead of on the host that has already lost its data.
    try:
        with tarfile.open(outfile) as probe:
            _refuse_oversized_archive(probe)
    except _ArchiveTooLarge as e:
        print(f"❌ {e}.")
        print(
            "   Refusing to upload a bundle this tool could not restore. The local "
            f"bundle is intact at {outfile} — narrow it with --components."
        )
        return 1
    except (tarfile.TarError, OSError) as e:
        print(f"❌ The bundle just written could not be re-read ({e}); not uploading.")
        return 1

    # What crosses the boundary is a REDACTED copy, keyed under the original's name so a
    # restore finds it where it expects. `mkdtemp` already creates the directory 0o700, so
    # the operator's memory is owner-only while it is staged here, and it is removed on
    # every path out.
    with tempfile.TemporaryDirectory(prefix="kirocrew-redact-") as tmpdir:
        workdir = Path(tmpdir)
        try:
            prepared = _redacted_upload_copy(outfile, workdir)
        except _RedactionFailed as e:
            print(f"❌ Refusing to upload: {e}.")
            print(
                "   Nothing was published. The local bundle is intact at "
                f"{outfile}. Redaction is required for uploads unless you set "
                "redaction off in your backup directory, which publishes credentials in "
                "plaintext."
            )
            _audit("snapshot_upload_rejected", f"reason=redaction_failed from={outfile.name}")
            return 1

        if prepared is None:
            _report_unredacted_upload()
            payload = outfile
        else:
            payload, report = prepared
            _report_redaction(report)

        # Escaped for DISPLAY only; the URL itself is built from the real name, because an
        # escaped name would name an object that does not exist. The bucket and prefix come
        # from the destination record, so the rendered line is not all ours.
        print(
            f"☁️  Uploading to "
            f"{_remote().safe_for_terminal(dest.url_for(outfile.name))} "
            f"(profile {profile})"
        )
        try:
            url = _remote().upload(payload, dest, profile, key_name=outfile.name)
        except _remote().UPLOAD_FAILURES as e:
            # Every failure the AWS path can raise becomes one controlled message. A
            # traceback here would be indistinguishable from a crash, and the operator
            # still has a usable local bundle either way.
            print(f"❌ {type(e).__name__}: {e}")
            print(f"   The local bundle is intact at {outfile}")
            return 1
    print(f"✅ Uploaded: {url}")
    _audit("snapshot_uploaded", url)
    return 0


def snapshot_main(
    argv: list[str] | None = None, *, parsed: argparse.Namespace | None = None
) -> int:
    if parsed is None:
        p = argparse.ArgumentParser(
            prog="kirocrew-snapshot",
            description="Create a portable .tar.gz snapshot of KiroCrew state.",
        )
        p.add_argument("output_dir", nargs="?", default=_default_snapshot_dir())
        p.add_argument("--keep", type=int, default=7)
        p.add_argument("--list", action="store_true", dest="list_snapshots")
        p.add_argument("--components", default=None)
        p.add_argument("--purpose", default=Purpose.BACKUP.value)
        p.add_argument("--to-s3", action="store_true", dest="to_s3")
        p.add_argument("--to", default=None, help=argparse.SUPPRESS)
        p.add_argument("--aws-profile", default=None, dest="aws_profile")
        parsed = p.parse_args(argv)
    args = parsed

    if args.keep <= 0:
        print(f"❌ --keep value must be a positive integer, got: {args.keep}")
        return 1

    # `--to s3://…` was replaced by a provisioned destination. Fail loudly rather than
    # letting an old invocation write the bundle into a local directory named `s3:`.
    if getattr(args, "to", None):
        print(
            f"❌ --to is no longer accepted (you passed {args.to!r}).\n"
            f"   A backup now writes only to a destination you provision once:\n"
            f"     kirocrew backup setup\n"
            f"     kirocrew snapshot --components memory --to-s3"
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

    # WAL checkpoint
    if (mc / "memory.db").is_file():
        try:
            with closing(sqlite3.connect(str(mc / "memory.db"))) as c:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            print(
                "⚠️  WAL checkpoint failed (DB may be locked by gateway). "
                "The backup API still produces a consistent copy."
            )

    try:
        with tempfile.TemporaryDirectory() as work:
            stage = Path(work) / root_name
            # Unconditionally, before any component runs. A file-only selection whose
            # files are all absent (a fresh home with `--components crons`) stages
            # nothing, and the manifest write below would then fail on a missing
            # directory — an empty bundle is a valid outcome, a crash is not.
            stage.mkdir(parents=True, exist_ok=True)

            # Files. A `.db` goes through the SQLite backup API so a live gateway holding
            # it open still yields a consistent copy; everything else is a plain copy.
            for comp in selected:
                for f in COMPONENTS[comp].files:
                    src = mc / f
                    if not src.is_file():
                        continue
                    if os.path.islink(src):
                        print(f"⚠️  Skipping symlinked core file: {src}")
                        continue
                    dst = stage / f
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if f.endswith(".db"):
                        with (
                            closing(sqlite3.connect(str(src))) as src_conn,
                            closing(sqlite3.connect(str(dst))) as dst_conn,
                        ):
                            # Same contract as the tree path: a failed consistent copy
                            # is reported by name at the command boundary, never
                            # absorbed and never a traceback.
                            try:
                                src_conn.backup(dst_conn)
                            except sqlite3.Error as e:
                                raise DatabaseCopyFailed(src, e) from e
                    else:
                        shutil.copy2(str(src), str(dst))

            # Trees. Selections overlap by design — `memory` names workspace/memory while
            # `workspace` names the whole tree — so staging is idempotent: dirs_exist_ok
            # plus copy2 means the second write of a path is identical to the first.
            for comp in selected:
                for tree in COMPONENTS[comp].trees:
                    src_dir = mc / tree
                    dst_dir = stage / tree
                    # An unsafe root must FAIL the snapshot, not be skipped. Skipping it
                    # produced the worst possible artefact: a bundle whose manifest declares
                    # `memory` while the markdown trees are silently absent, so the operator
                    # believes they are covered and only discovers otherwise when they try to
                    # recover. A backup that lies about its contents is worse than no backup.
                    #
                    # safe_tree_root returns None only for an unsafe or unresolvable root —
                    # a root that simply does not exist yet is fine — so this cannot fire on
                    # a fresh data home.
                    if safe_tree_root(src_dir, what="component root") is None:
                        raise UnsafeComponentRoot(
                            f"component {comp!r} names the tree {tree!r}, which does not "
                            f"resolve inside the data home. Refusing to write a bundle that "
                            f"would claim to contain {comp!r} without it — inspect that path "
                            f"(it is usually a symlink) and re-run."
                        )
                    if not src_dir.is_dir():
                        continue
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    _copytree_safe(
                        src_dir,
                        dst_dir,
                        dirs_exist_ok=True,
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
                    _restage_databases(src_dir, dst_dir)

            # Manifest
            ws_files = sum(1 for _ in (stage / "workspace").rglob("*") if _.is_file())
            pm_files = sum(1 for _ in (stage / "plan_memory").rglob("*") if _.is_file())
            sk_dir = stage / "skills"
            sk_count = sum(1 for _ in sk_dir.iterdir() if _.is_dir()) if sk_dir.is_dir() else 0
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

            # Tarball — write to temp file and rename atomically to avoid corrupt partials
            out.mkdir(parents=True, exist_ok=True)
            outfile = out / f"{name}.tar.gz"
            tmp_tar = outfile.with_suffix(".tar.gz.tmp")
            try:
                with tarfile.open(str(tmp_tar), "w:gz") as tar:
                    tar.add(str(stage), arcname=root_name, filter=_data_filter)
                tmp_tar.rename(outfile)
            except BaseException:
                tmp_tar.unlink(missing_ok=True)
                raise

    except UnsafeComponentRoot as e:
        # A selected component's tree does not resolve inside the data home. Refuse
        # with a message rather than a traceback: every other refusal on this path
        # already does, and a traceback here reads as a crash — which would invite
        # the operator to retry rather than to look at the path.
        print(f"❌ {e}")
        return 1
    except DatabaseCopyFailed as e:
        # The consistent copy of a live database failed, so no bundle is written. The
        # staging directory is a TemporaryDirectory and is already gone by here, so
        # nothing partial is left behind. Naming the file matters: the usual cause is a
        # concurrent exclusive writer, which the operator resolves by stopping whatever
        # holds that database rather than by re-running.
        print(f"❌ Could not copy {_safe_name(e.path.name)} consistently ({e}).")
        print(
            "   No bundle was written. Stop whatever is writing to that database "
            "(a running gateway is the usual cause) and re-run."
        )
        _audit("snapshot_failed", f"reason=database_copy path={e.path.name}")
        return 1
    sz = outfile.stat().st_size
    # restrict_to_owner (fail-loud), NOT chmod_safe: this tarball can contain
    # sel_hmac.key (see the warning below). chmod_safe swallows OSError and
    # would let the snapshot land group/world-readable while still printing
    # success. Fail loudly instead — better to abort than ship a
    # secret-bearing archive under-protected. POSIX applies chmod 0o600;
    # Windows applies an owner-only DACL via icacls.
    # Unlink+reraise on failure so the "abort" the comment promises actually
    # removes the exposed artifact — otherwise the tarball would sit on disk
    # with the destination's inherited DACL after a Python traceback.
    try:
        platform_compat.restrict_to_owner(str(outfile))
    except OSError:
        outfile.unlink(missing_ok=True)
        raise
    human = f"{sz // 1024}K" if sz < 1024 * 1024 else f"{sz / 1024 / 1024:.1f}M"

    # The bound belongs at CREATION, not only on the paths that move a bundle around. A
    # bundle past it cannot be restored by this tool, so reporting success would promise a
    # backup that does not exist — and the prune below would then delete older bundles that
    # DO restore in favour of one that never will. Checked before both.
    try:
        with tarfile.open(outfile) as probe:
            _refuse_oversized_archive(probe)
    except _ArchiveTooLarge as e:
        print(f"❌ {e}.")
        print(
            f"   The archive is written at {outfile}, and nothing was pruned — but this "
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

    # Off-host destination. Deliberately after the local bundle exists and is
    # owner-restricted: a failed upload must leave a usable local backup behind
    # rather than nothing.
    upload_rc = 0
    if getattr(args, "to_s3", False):
        upload_rc = _upload_bundle(outfile, args, selected)

    # Prune. This runs even when the upload failed, because --keep is a promise about
    # local disk and a persistently failing destination must not turn a daily backup
    # into an unbounded pile of bundles — the disk fills, and then the snapshot that
    # would have worked cannot be written either.
    snaps = sorted(
        out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True
    )
    for old in snaps[args.keep :]:
        old.unlink()
        print(f"🗑  Pruned: {_safe_name(old.name)}")

    remaining = len(list(out.glob("kirocrew-snapshot-*.tar.gz")))
    print(f"📦 Snapshots in {out}: {remaining} (keep={args.keep})")

    if upload_rc != 0:
        return upload_rc
    return 0


# ── Restore ───────────────────────────────────────────────────────────────────


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
            if safe_tree_root(d, what="destination root") is None:
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


def _print_manifest(snap: Path) -> None:
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
        # Every string here comes from the bundle, and a bundle can arrive from S3, so
        # it is untrusted input being written to a terminal. Escape sequences in a
        # manifest field can move the cursor, recolour, or overwrite what was already
        # printed -- letting a hostile archive dress itself up as a different, expected
        # one right above the prompt where the operator decides whether to restore it.
        # Numeric fields below are formatted as ints, so they cannot carry sequences.

        print("📋 Snapshot info:")
        print(f"  Created: {_safe_name(m.get('created_at'))}")
        print(f"  From: {_safe_name(m.get('user'))}@{_safe_name(m.get('hostname'))}")
        # Absent in bundles written before the purpose seam existed. Say so rather
        # than printing a default, so an old bundle is never read as a declared one.
        print(f"  Purpose: {_safe_name(m.get('purpose'), 'undeclared (pre-seam bundle)')}")
        comps = m.get("components")
        if isinstance(comps, dict) and comps:
            rendered = ", ".join(
                f"{_safe_name(k, '?')} [{_safe_name(v, '?')}]" for k, v in sorted(comps.items())
            )
            print(f"  Components: {rendered}")
        c = m.get("contents", {})
        print(f"  Memory DB: {c.get('memory_db', 0) // 1024} KB")
        print(f"  Crons: {c.get('crons_json', 0) // 1024} KB")
        print(f"  Workspace files: {c.get('workspace_files', 0)}")
        print(f"  Skills: {c.get('skill_count', 0)}")
        print(f"  Notifications: {c.get('notifications_jsonl', 0) // 1024} KB")
        print(f"  Plan memory files: {c.get('plan_memory_files', 0)}")
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


def _merge_crons(src_path: Path, dst_path: Path) -> None:
    # Cron job names are operator-authored text and routinely non-ASCII, so the
    # locale codepage is the wrong decoder for this file on any host.
    try:
        src = json.loads(src_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  Could not read {src_path}: {exc} — skipping cron merge")
        return
    try:
        dst = json.loads(dst_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  Could not read {dst_path}: {exc} — skipping cron merge")
        return
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


def _merge_notifications(src_path: Path, dst_path: Path) -> None:
    existing: set[str] = set()
    with open(dst_path) as f:
        for line in f:
            try:
                existing.add(json.loads(line).get("ts") or line.strip())
            except (ValueError, TypeError):
                pass
    imported = 0
    with open(dst_path, "a") as out, open(src_path) as f:
        for line in f:
            try:
                key = json.loads(line).get("ts") or line.strip()
                if key not in existing:
                    out.write(line)
                    existing.add(key)
                    imported += 1
            except (ValueError, TypeError):
                pass
    print(f"  Notifications imported: {imported}")


def _backup_and_copy(
    mc: Path, backup: Path, snap: Path, component: str, installed: set[str]
) -> None:
    """Save each core file aside, then install the incoming one.

    *installed* records every path this run begins writing, so recovery can tell a file
    the restore CREATED (no saved copy, safe to remove) from one it never reached (no
    saved copy either, and removing it would delete the operator's own data).
    """
    for f in CORE_FILES.get(component, ()):
        if (mc / f).is_file():
            if os.path.islink(mc / f):
                print(f"⚠️  Skipping symlinked core file during backup: {mc / f}")
                continue
            shutil.move(str(mc / f), str(backup / f))
        if (snap / f).is_file():
            if os.path.islink(snap / f):
                print(f"⚠️  Skipping symlinked file from snapshot: {snap / f}")
                continue
            # Marked BEFORE the copy, so a copy that fails halfway still counts as
            # installed and its partial file is removed rather than left standing.
            installed.add(f)
            shutil.copy2(str(snap / f), str(mc / f))
            if component == "security":
                # restrict_to_owner (fail-loud), NOT chmod_safe (swallows OSError):
                # security files include sel_hmac.key. Mirrors the create path's
                # deliberate fail-loud lockdown — better to abort than silently
                # land a restored secret group/world-readable. POSIX applies
                # chmod 0o600; Windows applies an owner-only DACL via icacls.
                # Unlink the freshly
                # copied file on failure so the "abort" the comment promises
                # actually removes the exposed artifact — otherwise the
                # restored secret would sit under the destination-inherited
                # DACL after the OSError propagates out of _do_replace.
                try:
                    platform_compat.restrict_to_owner(str(mc / f))
                except OSError:
                    (mc / f).unlink(missing_ok=True)
                    raise


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
            if safe_tree_root(d, what="destination root") is None:
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
                _refuse_unless_json_object(src, name, installed=mc_for_merge is None)

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
    empty — refusing is the only way the operator hears about it. Merge never installs it:
    the per-component merger reads the bundle's copy, reports a file it cannot parse, and
    returns without writing live state, so the content is neither lost nor lost quietly.
    Refusing there would turn one unreadable component into a failed restore of every
    other one, which is the opposite of what an off-host backup is for.

    Only structure is checked, not schema. Parsing proves the file survived transport and
    is the shape its consumer branches on; asserting field-level schema here would
    duplicate each consumer's own validation and refuse bundles those consumers accept.

    The shape checks below run in BOTH modes, and that asymmetry with the parse branch is
    deliberate: a merger's own guard catches a file it cannot parse, not one that parses
    into the wrong shape. `{"jobs": ["x"]}` parses, so it flows past that guard and reads
    fields off a `str` partway through the merge — past the point where refusing is free.
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
    if not isinstance(parsed, dict):
        raise SourceComponentUnsound(
            f"{label} in this snapshot is a JSON {type(parsed).__name__}, not an "
            "object.\n"
            "   Refusing to restore it over live state: its reader expects an object and "
            "treats anything else as empty."
        )
    # An object at the top is necessary and not sufficient. The readers iterate a named
    # list and call `.get` on each entry, so a `jobs` that is not a list of objects
    # reaches attribute access on a `str` and raises mid-merge — past the point where
    # refusing is free. Validate the shape the CONSUMER requires, not just the outer type.
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


def _do_replace(snap: Path, mc: Path, components: list[str] | None) -> None:
    _refuse_unsafe_destination_roots(mc, components)
    backup = _allocate_rollback_dir(mc)
    print("🔄 Replace mode — backing up current state...")

    # The memory trees are saved to the rollback directory HERE, before the loop below
    # swaps any database. An earlier revision computed these roots and copied them after
    # that loop, and the comment on the two-pass split even said so out loud — "the
    # databases were replaced before this block even started" — while only fixing the
    # tree-versus-tree ordering. Saving a tree after the databases have already moved
    # means a failure in this copy (a full disk is enough) leaves the rollback set
    # incomplete for state that is ALREADY replaced: memory half old, half new, with no
    # complete copy of either. A rollback set has to be finished before the first
    # mutation, not alongside it.
    #
    # Scoped by the same guard the replace pass uses: when `workspace` is also selected
    # its own pass owns these paths, and copying them twice would save the incoming tree
    # over the saved original.
    mem_roots: list[tuple[str, Path]] = []
    if _want(components, "memory") and not _want(components, "workspace"):
        for tree in COMPONENTS["memory"].trees:
            d = mc / tree
            if safe_tree_root(d, what="destination root") is None:
                continue
            mem_roots.append((tree, d))
        for tree, d in mem_roots:
            if d.is_dir():
                # No `dirs_exist_ok`: the rollback directory is named to the second, so
                # two restores inside one second resolve to the SAME directory. Merging
                # into it would blend two different pre-restore states into one rollback
                # set — the operator could not tell which files came from which restore,
                # and the set would roll back to neither. Colliding here raises, and
                # because this runs before the databases are swapped, the restore aborts
                # having changed nothing.
                _copytree_rollback(d, backup / tree)

    # The remaining trees are saved HERE too, before the mutation phase, for the same
    # reason and one sharper one. They used to be saved inside that phase, immediately
    # before each was replaced — which meant a save that failed PARTWAY (one unreadable
    # file is enough) raised into the recovery handler, and recovery then cleared the
    # intact live tree and put the PARTIAL copy back. An incomplete rollback set is worse
    # than none: recovering from it destroys data that was never touched.
    #
    # So the rule is the same one the memory trees already follow: the rollback set is
    # COMPLETE before anything mutates. A failure in this loop happens with the data home
    # still untouched, so it aborts and recovery never runs.
    #
    # No `dirs_exist_ok`: a collision means two different pre-restore states would blend
    # into one rollback set that restores to neither. It cannot happen on the honest path
    # (the memory-tree save above is skipped when `workspace` is selected, and the names
    # here are distinct), so a collision is a bug and fails closed.
    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            d = mc / dirname
            if d.is_dir():
                _copytree_rollback(d, backup / dirname)
    if _want(components, "skills"):
        sk = mc / "skills"
        if sk.is_dir():
            _copytree_rollback(sk, backup / "skills")

    # Everything past this point MUTATES the data home, and the rollback set above is
    # complete. A failure anywhere in the phase — a database swap, the first tree, the
    # third — leaves the home partly on the incoming generation and partly on the old
    # one, so recovery restores the WHOLE saved set rather than the item that failed.
    # Recovering only the failing item is what leaves memory split across two restore
    # generations: earlier trees stay replaced and the databases stay swapped.
    # Every relative path the mutation phase can write. Recovery needs it because a
    # target that did not exist before the restore has nothing saved for it, so putting
    # saved entries back would leave that creation standing.
    targets: list[str] = []
    for comp in ("memory", "crons", "config", "notifications", "security"):
        if _want(components, comp):
            targets.extend(COMPONENTS[comp].files)
    if _want(components, "workspace"):
        targets.extend(("workspace", "plan_memory"))
    if _want(components, "skills"):
        targets.append("skills")
    targets.extend(tree for tree, _ in mem_roots)

    # Grows as the phase touches each target; recovery reads it to tell a creation from
    # a target the phase never reached.
    installed: set[str] = set()
    try:
        _do_replace_mutations(snap, mc, backup, components, mem_roots, installed)
    except (OSError, DatabaseCopyFailed) as e:
        failed = _restore_everything_from_rollback(backup, mc, targets, installed)
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


def _do_replace_mutations(
    snap: Path,
    mc: Path,
    backup: Path,
    components: list[str] | None,
    mem_roots: list[tuple[str, Path]],
    installed: set[str],
) -> None:
    """Every mutation replace mode performs, so one handler can cover all of them.

    *installed* accumulates every declared path this run begins writing. Recovery needs
    it because a file is saved by moving it aside at the moment of its own mutation, so
    "nothing saved" is ambiguous until you know whether the phase ever got there.
    """
    for comp in ("memory", "crons", "config", "notifications", "security"):
        if _want(components, comp):
            _backup_and_copy(mc, backup, snap, comp, installed)
            print(f"  ✅ {comp}")

    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            d = mc / dirname
            sd = snap / dirname
            installed.add(dirname)
            _replace_tree_root(d, sd, backup / dirname)
        print("  ✅ workspace")

    if _want(components, "skills"):
        sk = mc / "skills"
        snap_sk = snap / "skills"
        installed.add("skills")
        _replace_tree_root(sk, snap_sk, backup / "skills")
        print("  ✅ skills")

    if _want(components, "memory") and not _want(components, "workspace"):
        # Scoped to memory's own subtrees: selecting `memory` alone must not disturb
        # the rest of workspace/.
        #
        # Skipped entirely when `workspace` is also selected, and that guard is
        # load-bearing rather than an optimization. The workspace block above has
        # already saved the ORIGINAL tree to the rollback dir and replaced the live
        # one with incoming files, so re-running the copy here would save the
        # INCOMING memory over the saved original and destroy the only copy of what
        # was replaced. Workspace's own pass covers these paths anyway.
        #
        # The rollback copy of these trees was already taken at the top of this
        # function, before any database was swapped, so by the time control reaches
        # here a complete rollback set exists and only the replace pass is left.
        for tree, d in mem_roots:
            sd = snap / tree
            installed.add(tree)
            # Cleared UNCONDITIONALLY, then filled only if the archive carries it.
            # Clearing only when the archive had the tree meant a bundle without, say,
            # `workspace/knowledge` left the destination's own knowledge tree in place,
            # so a "replace" produced restored memory mixed with stale notes and still
            # reported success. Replace means the destination ends up matching the
            # archive; a tree the archive does not have is a tree the destination must
            # not keep. The rollback copy was taken before any database was swapped, so
            # the removed state is still recoverable.
            #
            # A root can pass containment and still be a LINK — a symlink pointing
            # somewhere else *inside* the data home resolves within it, so
            # safe_tree_root allows it. shutil.rmtree then raises OSError on that
            # link, and by this point the databases have already been replaced, so
            # the operator is left half-restored. Remove a link as a link and reserve
            # rmtree for real directories.
            # Cleared and refilled through the one chokepoint. Recovery on failure is
            # the caller's, over the whole rollback set.
            _replace_tree_root(d, sd, backup / tree)


def _replace_tree_root(dst: Path, src: Path | None, rollback: Path | None) -> None:
    """Clear *dst* and refill it from *src*. Raises on failure; does NOT roll back.

    THE chokepoint for replacing a tree during a restore. Four sites do this — component
    trees, workspace, plan_memory, skills — and the ordering is unavoidable: clearing has
    to precede the copy, because replace means the destination ends up matching the
    archive and a tree the archive lacks is a tree the destination must not keep.

    Rolling back is deliberately NOT done here. A per-tree rollback restores the tree
    that failed and leaves every EARLIER one replaced, with the databases already
    swapped — memory split across two restore generations, which is the state a rollback
    exists to prevent. The unit of atomicity is the whole restore, so the caller owns
    recovery (:func:`_restore_everything_from_rollback`) and this function only reports.

    *rollback* is accepted so the message can name where the previous state is, which is
    what an operator needs if the caller's recovery also fails.
    """
    _clear_tree_root(dst)
    if src is None or not src.is_dir():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copytree_safe(src, dst)
    except OSError as e:
        where = f" Previous state is under {rollback}." if rollback is not None else ""
        print(f"❌ Restoring {_safe_name(dst.name)} failed ({e}).{where}")
        raise


def _restore_everything_from_rollback(
    backup: Path, mc: Path, targets: list[str], installed: set[str]
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
        return
    print(f"↩️  Restoring the previous state from {backup} ...")
    failed: list[str] = []
    for rel in sorted(set(targets)):
        saved = backup / rel
        target = mc / rel
        try:
            if saved.is_dir():
                _clear_tree_root(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                # The RESTORE half of the rollback round-trip, so it needs the same
                # link-preserving copy the save used. With the outbound copy here, a link
                # the save faithfully recorded is dropped on the way back — after the live
                # tree has already been cleared, which is the one moment there is no other
                # copy of it.
                _copytree_rollback(saved, target)
            elif saved.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(saved), str(target))
            elif rel in installed and (
                target.exists() or platform_compat.is_link_or_junction(target)
            ):
                # Nothing saved AND this run put it here: undo the creation.
                if target.is_dir() and not platform_compat.is_link_or_junction(target):
                    _clear_tree_root(target)
                else:
                    target.unlink(missing_ok=True)
        except OSError as e:
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


def _do_merge(snap: Path, mc: Path, components: list[str] | None) -> None:
    _refuse_unsafe_destination_roots(mc, components)
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
                if safe_tree_root(dd, what="destination root") is None:
                    continue
                dd.mkdir(parents=True, exist_ok=True)
                _report_unmerged_databases(sd, dd, tree)
                _copy_tree_no_overwrite(sd, dd, mc)

    if _want(components, "crons"):
        sc, dc = snap / "crons.json", mc / "crons.json"
        if sc.is_file():
            if dc.is_file():
                _merge_crons(sc, dc)
            else:
                shutil.copy2(str(sc), str(dc))
                print("  Crons: copied (no existing crons)")
        print("  ✅ crons")

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
                shutil.copy2(str(s), str(d))
                # restrict_to_owner (fail-loud), NOT chmod_safe — security
                # files include sel_hmac.key; mirror the create path. Windows
                # applies an owner-only DACL via icacls. Unlink the freshly
                # copied file on
                # failure so an icacls error doesn't leave a restored secret
                # under the destination-inherited DACL.
                try:
                    platform_compat.restrict_to_owner(str(d))
                except OSError:
                    d.unlink(missing_ok=True)
                    raise
                print(f"  {f}: restored (was missing)")
        print("  ✅ security")

    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            sd = snap / dirname
            if sd.is_dir():
                dd = mc / dirname
                dd.mkdir(parents=True, exist_ok=True)
                _copy_tree_no_overwrite(sd, dd, mc)
        print("  ✅ workspace")

    if _want(components, "skills"):
        if (snap / "skills").is_dir():
            (mc / "skills").mkdir(parents=True, exist_ok=True)
            _copy_tree_no_overwrite(snap / "skills", mc / "skills", mc)
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
        parsed = p.parse_args(argv)
    args = parsed

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

    # An s3:// argument is fetched first, then treated exactly like a local bundle: the
    # extraction filter, the archive bound, and the source-database integrity refusal are
    # the validation, and a downloaded object is untrusted input regardless of whose
    # bucket it came from. All three run BEFORE any live state moves; the destination
    # integrity check further down reports on the result and is not what makes a bundle
    # safe to apply.
    #
    # It lands in the snapshots dir rather than a temp dir on purpose: an operator
    # recovering a dead host usually restores more than once, and keeping the fetched
    # bundle means the second attempt costs no transfer.
    # Set when this run fetched the bundle, so a bundle that turns out to be unusable can
    # be removed instead of lingering as the newest snapshot.
    downloaded: Path | None = None
    if str(args.snapshot).startswith("s3://"):
        try:
            profile, _region = _resolve_aws_profile(getattr(args, "aws_profile", None))
        except (DestinationUnresolved, OSError, ValueError) as e:
            print(f"❌ Could not resolve an AWS profile: {e}")
            return 1
        into = Path(_default_snapshot_dir())
        # Escaped BEFORE `parse_s3_url` has judged it: this is caller-supplied text on
        # its way to a terminal, and the validation that would reject a hostile value
        # happens after this line.
        print(f"☁️  Downloading {_safe_name(str(args.snapshot))} (profile {profile})")
        try:
            local = _remote().download(str(args.snapshot), into, profile)
        except _remote().UPLOAD_FAILURES as e:
            print(f"❌ {type(e).__name__}: {e}")
            return 1
        try:
            platform_compat.restrict_to_owner(str(local))
        except OSError as e:
            # The bundle arrived but could not be locked down to the owner. Remove it
            # rather than leaving a world-readable copy of the operator's memory on
            # disk, and report it — a traceback here is indistinguishable from a crash.
            local.unlink(missing_ok=True)
            print(f"❌ Could not restrict {local} to owner-only ({e}); removed the download.")
            return 1
        print(f"  Saved to {local}")
        # A downloaded object is untrusted input even from a bucket we own: the key was
        # named on the command line, versioning means an older object may be corrupt,
        # and a truncated transfer produces a file that only fails when opened. Verify
        # it is a readable archive HERE, where the download can still be removed,
        # rather than letting tarfile raise out of the extract path as a traceback that
        # is indistinguishable from a crash and leaves the bad file behind.
        try:
            with tarfile.open(local) as probe:
                _refuse_oversized_archive(probe)
        except _ArchiveTooLarge as e:
            local.unlink(missing_ok=True)
            print(f"❌ {e}; removed the download.")
            return 1
        except (tarfile.TarError, OSError, EOFError) as e:
            local.unlink(missing_ok=True)
            print(
                f"❌ The downloaded object is not a readable snapshot archive ({e}); "
                f"removed it. Check the key, or pick another bundle with "
                f"`kirocrew backup list`."
            )
            return 1
        args.snapshot = str(local)
        downloaded = local

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
                try:
                    tar.extractall(work, filter=_data_filter)
                except TypeError:
                    # Python < 3.11.4: filter param not supported, apply manually
                    members = [m for m in tar.getmembers() if _data_filter(m) is not None]
                    tar.extractall(work, members=members)
        except _ArchiveTooLarge as e:
            _audit(
                "state_restore_rejected",
                f"reason=archive_too_large from={snap_path.name}",
            )
            removed = ""
            if downloaded is not None:
                downloaded.unlink(missing_ok=True)
                removed = " Removed the download."
            print(f"❌ {e}.{removed}\n   Nothing was restored.")
            return 1
        except (tarfile.TarError, OSError, EOFError) as e:
            _audit(
                "state_restore_rejected",
                f"reason=extraction_failed from={snap_path.name}",
            )
            removed = ""
            if downloaded is not None:
                # A fetched bundle is kept in the snapshots dir so a second attempt costs
                # no transfer, which means leaving a broken one behind makes it the NEWEST
                # snapshot — the one a bare `kirocrew restore` then selects every time.
                downloaded.unlink(missing_ok=True)
                removed = " Removed the download."
            print(
                f"❌ This snapshot could not be extracted ({e}).{removed}\n"
                "   Nothing was restored."
            )
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
                components = declared
                print(
                    "🔧 Components (from bundle manifest): "
                    f"{','.join(components) if components else '(none)'}"
                )
        elif declared is not None:
            # An explicit selection the bundle does not contain is a refusal, not a
            # no-op: replace mode would move the live files of that component out to
            # the rollback dir and have nothing to put back.
            absent = [c for c in components if c not in declared]
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
        try:
            if mode == "replace":
                _do_replace(snap, mc, components)
            else:
                _do_merge(snap, mc, components)
        except UnsafeComponentRoot as e:
            # Raised before anything was written, so this is a clean refusal. Report it
            # as one rather than letting a traceback out — the same contract every other
            # refusal on this path already follows.
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
            # fails MID-mutation, which is a different answer from the refusal above:
            # `_do_replace` has already put the whole saved set back and re-raised. So the
            # home is on its pre-restore generation and the operator needs to be told that
            # much — a traceback says a restore blew up without saying what state they are
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
            # installed — and leaves the restored file held open either way.
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
