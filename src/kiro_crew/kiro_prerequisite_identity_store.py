"""Kiro's identity stores: where they are, how they are read, and how they are staged.

The fixed, home-anchored location of kiro-cli's identity database, the bounded and
symlink-refusing readers for it, and the credential-minimal HOME (identity tables
projected, nothing else) that a sandboxed auth call runs against.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
import urllib.request
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import identity_stores, platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.atomic_write import atomic_write
from kiro_crew.owner_only_files import ensure_directory

_AUTH_STAGING_RELATIVE = Path(".kiro") / "crew-auth-staging"


_MAX_AUTH_STORE_FILE_BYTES = 64 * 1024 * 1024
_AUTH_STORE_READ_ERROR = "Kiro identity file could not be read safely"
# The Kiro CLI identity database is PROJECTED, never byte-copied. It is the CLI's
# main store: identity lives in two small tables, while `history` /
# `conversations*` hold chat transcripts and grow without bound (a real user
# report had it at ~429 MB). A byte copy therefore (a) blew past
# _MAX_AUTH_STORE_FILE_BYTES and aborted sign-in with a message that names
# neither size nor the real cause, and (b) read the whole file into memory to
# write it straight back out. Projection copies the full SCHEMA plus rows from
# _AUTH_IDENTITY_TABLES only, so the staged database is small and bounded by the
# identity data alone, no matter how large the source grows.
#
# Full schema, not just the identity tables: `migrations` is projected WITH its
# rows, so the CLI sees its schema version as already-applied and runs no
# migration. A database holding only the identity tables would then fail with
# "no such table: history" on first use. Copying every table's DDL (and indexes)
# while withholding non-identity ROWS keeps the CLI's queries valid and still
# hands the sandboxed process no transcript content.
_AUTH_SQLITE_DB = identity_stores.AUTH_SQLITE_DB
_AUTH_IDENTITY_TABLES = ("auth_kv", "migrations")
# `state` is a mixed key/value table: a few rows describe WHICH identity is signed
# in (Identity Center region + start URL, CodeWhisperer profile) and the rest is
# unrelated local state — telemetry ids, onboarding flags, prompt counters. Copy
# the table's rows SELECTIVELY by key prefix so the staged store can render a
# complete `whoami` (profile/ARN block) without also handing the sandboxed CLI
# the user's telemetry identifiers. Prefix-matched, not an exact list, so a new
# `auth.idc.*` key is carried automatically rather than silently dropped.
_AUTH_STATE_TABLE = "state"
_AUTH_STATE_KEY_PREFIXES = ("auth.", "api.codewhisperer.")
_AUTH_SQLITE_FILES = (_AUTH_SQLITE_DB,)


# Short: the projection is a handful of small reads against a local file, and a
# stuck open must not hold up sign-in.
_AUTH_SQLITE_TIMEOUT_SECS = 5.0


@dataclass(frozen=True)
class _AuthStoreMapping:
    """One real Kiro identity store mapped into the temporary auth home."""

    source: Path
    staged_relative: Path
    filenames: tuple[str, ...]
    # Mappings sharing a group are ALTERNATE locations of ONE store, only one of
    # which a given host uses. Staging must abort when a matched store cannot be
    # read, because a staged home with no identity looks signed-out -- but that
    # rule is right per LOCATION and wrong across alternates, where a stale
    # leftover in the root this host abandoned would abort staging from the root
    # it actually uses. Within a group the abort is therefore deferred: it fires
    # only when NO alternate yielded an identity. ``None`` means "not an
    # alternate of anything" and keeps the strict per-location rule -- the AWS
    # SSO cache is a single location holding several token files, and losing any
    # one of those must still abort.
    group: str | None = None


@dataclass(frozen=True)
class _AuthWorkspace:
    """Temporary credential-minimal home for a trusted Kiro CLI auth call."""

    root: Path
    env: dict[str, str]


def _read_bounded_regular_file(path: Path) -> bytes | None:
    """Read an allowlisted auth-store file with size and symlink defenses."""

    if path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_AUTH_STORE_FILE_BYTES:
            return None
        output = bytearray()
        while len(output) <= _MAX_AUTH_STORE_FILE_BYTES:
            chunk = os.read(fd, min(1024 * 1024, _MAX_AUTH_STORE_FILE_BYTES + 1 - len(output)))
            if not chunk:
                return bytes(output)
            output.extend(chunk)
        return None
    finally:
        os.close(fd)


def _open_identity_db_readonly(path: Path) -> Any | None:
    """Open a Kiro identity database read-only, with the same path defenses.

    Mirrors :func:`_read_bounded_regular_file`'s gates — reject a symlink, and
    require a regular file — then hands SQLite a read-only URI. Deliberately NOT
    size-gated: only the identity tables are read out, so the source file's size
    does not bound what is staged. Returns ``None`` when the path fails a gate or
    SQLite cannot read it (missing, corrupt, or not a database).
    """

    if path.is_symlink():
        return None
    try:
        if not stat.S_ISREG(os.lstat(str(path)).st_mode):
            return None
    except OSError:
        return None
    # mode=ro WITHOUT immutable=1. `immutable=1` would promise never to touch a
    # sidecar, but it also tells SQLite the file cannot change, so the WAL is
    # IGNORED: against a store in WAL mode whose newest commits are still in
    # `data.sqlite3-wal`, the token row reads as missing and the CLI is handed a
    # store it reports as signed-out — a worse failure than the size abort this
    # projection replaces. Plain `mode=ro` applies the WAL, so the staged
    # identity always matches what the CLI itself would read. The cost is that
    # SQLite may create/refresh the `-shm` index beside the live database exactly
    # as any other reader does; `-shm` is a shared-memory index holding no
    # identity data, and no identity bytes are ever written back.
    uri = f"file:{urllib.request.pathname2url(str(path))}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=_AUTH_SQLITE_TIMEOUT_SECS)
    except sqlite3.Error:
        return None


def _project_identity_database(source: Path, destination: Path) -> bool:
    """Stage ONLY Kiro's identity tables into a fresh owner-only database.

    Copies every table/index DDL (so the CLI never hits "no such table" on a
    store whose ``migrations`` rows say the schema is current) but only the ROWS
    of :data:`_AUTH_IDENTITY_TABLES`. Transcript tables arrive empty, so the
    staged file stays small however large the source grows.

    Returns ``False`` when the source cannot be read or lacks an identity table,
    so the caller aborts staging instead of running against a store that looks
    signed-out.
    """

    connection = _open_identity_db_readonly(source)
    if connection is None:
        return False
    try:
        with contextlib.closing(connection):
            schema = connection.execute(
                "SELECT sql FROM sqlite_master "  # wokeignore:rule=master
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"  # wokeignore:rule=master
                ).fetchall()
            }
            # EVERY identity table must exist. `all`, not `any`: a future kiro-cli
            # that renames one of them (say `auth_kv`) while keeping the other
            # would pass an `any` gate and stage a store with the schema present
            # but ZERO identity rows — silently producing exactly the "signed out"
            # outcome this gate exists to prevent. Requiring all of them turns a
            # schema change into the loud _AUTH_STORE_READ_ERROR abort instead.
            if not all(table in present for table in _AUTH_IDENTITY_TABLES):
                return False
            rows = {
                table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
                for table in _AUTH_IDENTITY_TABLES
            }
            # `state`: carry only the identity-describing keys (see
            # _AUTH_STATE_KEY_PREFIXES). Absent on an older schema — optional by
            # design, so a store without it still stages.
            if _AUTH_STATE_TABLE in present:
                predicate = " OR ".join(["key LIKE ?"] * len(_AUTH_STATE_KEY_PREFIXES))
                rows[_AUTH_STATE_TABLE] = connection.execute(
                    f'SELECT * FROM "{_AUTH_STATE_TABLE}" WHERE {predicate}',
                    tuple(f"{prefix}%" for prefix in _AUTH_STATE_KEY_PREFIXES),
                ).fetchall()
    except sqlite3.Error:
        return False

    ensure_directory(destination.parent)  # 0700 inside the data home
    # Create the file before writing, so the identity rows are never briefly
    # world-readable between SQLite's create and the chmod.
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    platform_compat.restrict_to_owner(str(destination))
    try:
        staged = sqlite3.connect(str(destination), timeout=_AUTH_SQLITE_TIMEOUT_SECS)
        with contextlib.closing(staged):
            with staged:
                for (statement,) in schema:
                    staged.execute(str(statement))
                for table, table_rows in rows.items():
                    if not table_rows:
                        continue
                    placeholders = ",".join("?" * len(table_rows[0]))
                    staged.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', table_rows)
    except sqlite3.Error:
        with contextlib.suppress(OSError):
            os.unlink(str(destination))
        return False
    return True


def _atomic_write_secret_bytes(path: Path, content: bytes) -> None:
    """Atomically stage one bounded Kiro identity file, owner-only from birth.

    ``restrict_to_owner=True`` locks the staged temp file down BEFORE the
    identity bytes reach it (``0o600`` on POSIX, an owner-only DACL on
    Windows), so those bytes never sit in a file readable at the parent's
    inherited DACL. The default ``restrict_on_error="raise"`` keeps that
    unconditional: a lockdown failure aborts before the final path is touched,
    so an unprotectable identity file never exists there at all.
    """

    atomic_write(path, content, fsync=True, restrict_to_owner=True)


def kiro_identity_store_path(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> Path:
    """Return kiro-cli's OWN live identity database path.

    Deliberately not amazon-q's: that store often holds the same account but is a
    different product's credential, so it cannot answer "which account is this
    CLI signed in as".

    Every platform resolves among FIXED, home-anchored locations, drawn from the
    same set as the trusted live-store list in
    ``dashboard/handlers/kiro_usage_api.py`` (``_CLI_SQLITE_DBS``). No
    environment variable is consulted -- not ``XDG_DATA_HOME`` on Linux, not
    ``APPDATA`` or ``LOCALAPPDATA`` on Windows -- because the fence that makes
    this store unwritable by agent file tools (``_SENSITIVE_HOME_DIRS``) is
    anchored at exactly these paths. A redirected location either resolves to the
    same place or falls OUTSIDE the fence, where an agent can author the rows
    this function reads: it could then forge an identity that keeps matching and
    the children signed in as the previous account would never be retired. A
    fixed anchor cannot be pointed at something the agent may write.

    On Windows current kiro-cli writes its store under the local app-data
    directory (``AppData/Local/kiro-cli``); older layouts used the roaming one
    (``AppData/Roaming/kiro-cli``). When only one store exists it is chosen;
    when both exist the most recently written one wins, so a leftover from
    the other layout never masks the live store's account (the WAL-aware
    mtime tie-break lives in :func:`identity_stores.selected_store`). Both
    anchors sit inside the
    ``_SENSITIVE_HOME_DIRS`` fence, so neither choice widens what an agent
    can forge. This branch stats the filesystem, so callers on the event loop
    should resolve the path inside the same worker thread as the read itself.

    The cost is that a host which relocates its data home is read as having no
    identity, so the change is reported as "absent" -- which errs toward retiring
    children, never toward trusting them. ``environ`` is kept in the signature so
    callers need not know which platforms consult it, and so a future platform
    that genuinely requires it does not change every call site.
    """

    return identity_stores.selected_store(platform_name, home)


def identity_store_is_relocated(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> bool:
    """Whether the CLI's data home is configured somewhere other than our anchor.

    :func:`kiro_identity_store_path` deliberately reads a FIXED path, because the
    fence that keeps agent file tools out of that store is home-anchored and a
    redirected path can land outside it. But refusing to follow the redirection is
    not the same as being safe: if the environment points the CLI elsewhere and a
    LEFTOVER database still sits at the default location, reading it yields a
    confident fingerprint of an account nobody is signed into. A logout in the real
    store would then change nothing we can see, and the old-account child would be
    reused -- strictly worse than reporting "cannot tell".

    So a configured relocation is reported here, and the caller treats it as
    absent: no read, no false confidence, and the absent path already means "never
    reconciled, re-sweep every turn".

    Both variables count, unconditionally. The live store's directory is
    resolved by the CLI from ``LOCALAPPDATA`` (current layout) or ``APPDATA``
    (legacy layout), and which generation is writing cannot be observed --
    so once EITHER variable is redirected, a database at a fixed anchor
    cannot be attributed to a live writer: it may be the live store of the
    other generation, or a leftover of either, and reading a leftover yields
    a confident fingerprint of an account nobody is signed into. Refusing to
    guess errs toward absent, the module's safe side. The cost is that a
    host with Group-Policy folder redirection (which targets Roaming)
    reports absent even when a current-layout Local store is healthy -- the
    same answer such hosts got when the anchor lived under Roaming, so the
    posture is status quo there, and the once-per-service log in
    :meth:`KiroPrerequisiteService.current_identity_fingerprint` makes it
    diagnosable. A variable set to exactly the default location is not a
    relocation.

    The (variable, default root) pairs are PROJECTED from
    :data:`identity_stores.IDENTITY_STORE_ROOTS` -- each kiro-cli row's
    ``env_var`` against the parent of its home-relative directory -- so this
    check learns about a new or relocated store row the same way the fence,
    staging, and state-db discovery do. macOS falls out naturally: its rows
    carry no ``env_var`` (no standard variable relocates
    ``~/Library/Application Support``), so no pair is checked there.
    """

    plat = (
        identity_stores.Platform(platform_name)
        if platform_name in ("darwin", "win32")
        else identity_stores.Platform.POSIX
    )
    for root in identity_stores.IDENTITY_STORE_ROOTS:
        if (
            root.platform is not plat
            or root.product is not identity_stores.Product.KIRO_CLI
            or root.env_var is None
        ):
            continue
        default = home.joinpath(*root.home_relative_dir.split("/")).parent
        configured = environ.get(root.env_var, "").strip()
        if configured and Path(configured) != default:
            return True
    return False


def _auth_store_mappings(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> tuple[_AuthStoreMapping, ...]:
    """Return only Kiro identity stores, never the surrounding credential dirs.

    Built over the canonical :func:`identity_stores.store_mappings` projection so
    the source directories, the env-var source-side honouring
    (``LOCALAPPDATA`` / ``APPDATA`` / ``XDG_DATA_HOME``), the fixed staged side,
    and the Local-before-Roaming ordering all come from the single table. This
    wrapper keeps staging's own concerns: the ``.aws/sso/cache`` token mapping
    (not an identity store, so not in the table), the ``_AuthStoreMapping`` shape
    with ``filenames=_AUTH_SQLITE_FILES``, and the ``win32:{app}`` group that
    defers the abort across a product's two alternate AppData roots.
    """

    mappings = [
        _AuthStoreMapping(
            source=home / ".aws" / "sso" / "cache",
            staged_relative=Path(".aws") / "sso" / "cache",
            filenames=("kiro-auth-token*.json",),
        )
    ]
    for row in identity_stores.store_mappings(platform_name, home, environ):
        # Both AppData roots of one Windows product are alternates: only one is
        # this host's live store, so a stale leftover in the unused root must
        # not abort staging from the used one. A shared group defers the abort
        # across them; distinct ``staged_relative`` values (from the table) keep
        # them from overwriting each other. macOS/Linux have a single location
        # per product, so no group. (The env-var source-side honouring and the
        # fixed staged side are already applied by ``store_mappings``.)
        group = f"win32:{row.product.value}" if platform_name == "win32" else None
        mappings.append(
            _AuthStoreMapping(
                source=row.source,
                staged_relative=row.staged_relative,
                filenames=_AUTH_SQLITE_FILES,
                group=group,
            )
        )
    return tuple(mappings)


def _ensure_auth_staging_parent(home: Path) -> Path:
    """Create the fixed sandbox-hidden parent before agent sessions can start."""

    staging_parent = home / _AUTH_STAGING_RELATIVE
    # A stray file or dangling/loop symlink at this path makes a plain
    # mkdir(exist_ok=True) raise FileExistsError and crash gateway boot. Remove
    # it before mkdir so boot self-heals. UNLINK rather than rename-aside: the
    # staging root holds credential material, and moving an unexpected file to a
    # sibling name would leave its (possibly sensitive) contents readable outside
    # the sandbox-hidden staging prefix.
    #
    # The predicate is ``is_link_or_junction``, not ``Path.is_symlink()``: a
    # Windows directory junction answers ``is_symlink() == False`` and
    # ``is_dir() == True``, so a symlink-only check certifies a planted junction
    # as the private root and stages credential material THROUGH it into the
    # junction's target. The removal is ``unlink_link_or_junction``, its
    # documented pair: it removes a symlink with ``unlink`` and a junction with
    # ``rmdir``, so the reparse point itself is cleared and its target is left
    # untouched. On POSIX the two match ``islink`` / ``unlink``.
    if platform_compat.is_link_or_junction(staging_parent) or (
        staging_parent.exists() and not staging_parent.is_dir()
    ):
        try:
            platform_compat.unlink_link_or_junction(str(staging_parent))
        except OSError as exc:
            # A concurrent gateway boot may win the race and clear the stray path
            # first (FileNotFoundError), or replace it with the real private
            # directory. Only abort if a non-directory we cannot clear is still
            # sitting here; otherwise fall through to the idempotent mkdir.
            if platform_compat.is_link_or_junction(staging_parent) or (
                staging_parent.exists() and not staging_parent.is_dir()
            ):
                raise OSError(
                    f"Kiro auth staging root {staging_parent} is not a private "
                    "directory and could not be reset"
                ) from exc
    staging_parent.mkdir(parents=True, exist_ok=True)
    if platform_compat.is_link_or_junction(staging_parent) or not staging_parent.is_dir():
        raise OSError("Kiro auth staging root is not a private directory")
    if platform_compat.IS_POSIX:
        platform_compat.chmod_safe(str(staging_parent), 0o700)
    else:
        platform_compat.restrict_dir_to_owner(str(staging_parent))
    return staging_parent


def _prepare_auth_workspace(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
    base_env: dict[str, str],
) -> _AuthWorkspace:
    """Build a sandbox-hidden HOME containing only Kiro identity artifacts."""

    staging_parent = _ensure_auth_staging_parent(home)
    root = Path(tempfile.mkdtemp(prefix="auth-", dir=str(staging_parent)))
    try:
        if platform_compat.IS_POSIX:
            platform_compat.chmod_safe(str(root), 0o700)
        else:
            platform_compat.restrict_dir_to_owner(str(root))
        # Deferred aborts, keyed by group: a group records its failure and is
        # judged only after every alternate has been attempted.
        group_staged: dict[str, bool] = {}
        group_failed: dict[str, str] = {}
        for mapping in _auth_store_mappings(platform_name, home, environ):
            if mapping.group is not None:
                group_staged.setdefault(mapping.group, False)
            for pattern in mapping.filenames:
                for source in mapping.source.glob(pattern):
                    staged_path = root / mapping.staged_relative / source.name
                    # The identity DATABASE is projected (identity tables only);
                    # every other identity file is a small JSON token copied
                    # under the bounded byte rules. Both abort staging on
                    # failure — never omit a matched store as though absent.
                    # For a mapping in a GROUP the abort is deferred to the
                    # group verdict below, because the alternates of one store
                    # are not each independently required.
                    if source.name == _AUTH_SQLITE_DB:
                        if not _project_identity_database(source, staged_path):
                            if mapping.group is None:
                                raise OSError(_AUTH_STORE_READ_ERROR)
                            group_failed[mapping.group] = _AUTH_STORE_READ_ERROR
                            continue
                        if mapping.group is not None:
                            group_staged[mapping.group] = True
                        continue
                    content = _read_bounded_regular_file(source)
                    if content is None:
                        if mapping.group is None:
                            raise OSError(_AUTH_STORE_READ_ERROR)
                        group_failed[mapping.group] = _AUTH_STORE_READ_ERROR
                        continue
                    _atomic_write_secret_bytes(staged_path, content)
                    if mapping.group is not None:
                        group_staged[mapping.group] = True

        # A group that had a readable store in ANY of its alternates is staged.
        # A group whose every matched store failed is the signed-out-looking
        # case the strict rule exists for, so it still aborts.
        for group, message in group_failed.items():
            if not group_staged.get(group, False):
                raise OSError(message)

        env = dict(base_env)
        env.update(
            {
                "HOME": str(root),
                "USERPROFILE": str(root),
                "XDG_CACHE_HOME": str(root / ".cache"),
                "XDG_CONFIG_HOME": str(root / ".config"),
                "XDG_DATA_HOME": str(root / ".local" / "share"),
                "APPDATA": str(root / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(root / "AppData" / "Local"),
            }
        )
        return _AuthWorkspace(root=root, env=env)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
