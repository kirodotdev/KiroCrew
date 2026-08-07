"""App Manager — install, uninstall, enable, disable lifecycle for KiroCrew apps.

Apps are installed to ``~/.kiro/crew/apps/{name}/``.  Each installed app has an
``installed.json`` metadata file tracking version, timestamp, and enabled state.

The manager validates manifests, copies app files, and delegates resource
registration (agents, skills, crons) to bridge functions.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import stat
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.execution import (
    app_execution_denied,
    shipped_builtin_app_root,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.version import check_min_version, parse_version
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.platform import current_context, safe_context_call
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

APP_MANIFEST_FILENAME = "app.json"
INSTALLED_META_FILENAME = "installed.json"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def apps_dir() -> Path:
    """Return the root directory for installed apps: ``~/.kiro/crew/apps/``."""
    return config_dir() / "apps"


def app_dir(name: str) -> Path:
    """Return the directory for a specific installed app."""
    return apps_dir() / name


def app_data_dir(name: str) -> Path:
    """Return the app-scoped data directory: ``~/.kiro/crew/apps/{name}/data/``."""
    d = app_dir(name) / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Installed metadata
# ---------------------------------------------------------------------------

# Valid values for InstalledApp classification fields
_VALID_ORIGIN: frozenset[str] = frozenset({"builtin", "registry", "local", "external"})
_VALID_RESOURCES: frozenset[str] = frozenset({"gateway", "app"})
_VALID_LIFECYCLE: frozenset[str] = frozenset({"gateway", "app", "locked"})


@dataclass
class InstalledApp:
    """Metadata persisted in ``installed.json`` for each installed app.

    Three orthogonal classification fields replace the old ``managed`` field:

    ``origin`` — where the app came from (read-only, set at install time):
      - ``"builtin"``: baked into the KiroCrew dashboard
      - ``"registry"``: installed from the curated app registry
      - ``"local"``: installed from a local directory path
      - ``"external"``: self-registered via SDK / API

    ``resources`` — who manages agent/skill/cron registration:
      - ``"gateway"``: KiroCrew manages via bridges.py symlinks
      - ``"app"``: the app manages its own resource registration

    ``lifecycle`` — who manages updates and uninstall:
      - ``"gateway"``: KiroCrew handles updates and uninstall
      - ``"app"``: the app handles its own updates
      - ``"locked"``: cannot be uninstalled (builtin only)
    """

    name: str = ""
    version: str = ""
    displayName: str = ""  # noqa: N815
    enabled: bool = True
    installedAt: str = ""  # noqa: N815
    updatedAt: str = ""  # noqa: N815
    source: str = ""  # concrete provenance: path, URL, "registry:name", "builtin"
    origin: str = "registry"  # builtin | registry | local | external
    resources: str = "gateway"  # gateway | app
    lifecycle: str = "gateway"  # gateway | app | locked
    schemaVersion: int = 2  # noqa: N815  — schema version for future migrations
    migratedTo: str = (
        ""  # noqa: N815  — target standalone app: "registry:{name}" or "standalone:{name}"
    )
    dev: bool = False  # dev mode: no-store UI serving + file-watch live reload
    # Structured install provenance, recorded for registry installs (see
    # ``set_app_provenance``).  ``source`` alone is a bare ``registry:<name>``
    # marker that re-resolves by name, so a same-named entry from a different
    # registry source could answer for this app; these fields pin WHICH source it
    # actually came from.  ``sourceUrl`` is the presence discriminator: empty
    # means a legacy record installed before provenance was captured (an empty
    # ``sourceRegistry`` is meaningful on its own — it denotes the bundled
    # catalog rather than a configured external registry).
    sourceUrl: str = ""  # noqa: N815  — git URL this app was installed from
    sourceRegistry: str = ""  # noqa: N815  — external registry id; "" = bundled catalog
    sourceCommit: str = ""  # noqa: N815  — commit SHA resolved in the source clone
    sourceSigner: str = ""  # noqa: N815  — verified signer id; "" = no verified signature

    def validate_fields(self) -> list[str]:
        """Validate classification field values. Returns error list (empty = valid)."""
        errors: list[str] = []
        if self.origin not in _VALID_ORIGIN:
            errors.append(f"invalid origin: {self.origin!r}")
        if self.resources not in _VALID_RESOURCES:
            errors.append(f"invalid resources: {self.resources!r}")
        if self.lifecycle not in _VALID_LIFECYCLE:
            errors.append(f"invalid lifecycle: {self.lifecycle!r}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v or isinstance(v, (bool, int))}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstalledApp:
        inst = cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            displayName=str(data.get("displayName", "")),
            enabled=bool(data.get("enabled", True)),
            installedAt=str(data.get("installedAt", "")),
            updatedAt=str(data.get("updatedAt", "")),
            source=str(data.get("source", "")),
            origin=str(data.get("origin", "registry")),
            resources=str(data.get("resources", "gateway")),
            lifecycle=str(data.get("lifecycle", "gateway")),
            schemaVersion=int(data.get("schemaVersion", 1)),
            migratedTo=str(data.get("migratedTo", "")),
            dev=bool(data.get("dev", False)),
            sourceUrl=str(data.get("sourceUrl", "")),
            sourceRegistry=str(data.get("sourceRegistry", "")),
            sourceCommit=str(data.get("sourceCommit", "")),
            sourceSigner=str(data.get("sourceSigner", "")),
        )
        # Migrate old "managed" field to new classification fields
        if inst.schemaVersion < 2 and "origin" not in data:
            old_managed = data.get("managed", "")
            if old_managed == "self":
                inst.origin = "external"
                inst.resources = "app"
                inst.lifecycle = "app"
            elif old_managed == "builtin":
                inst.origin = "builtin"
                inst.resources = "gateway"
                inst.lifecycle = "locked"
            elif old_managed in ("kirocrew", ""):
                source = data.get("source", "")
                if source.startswith("registry:"):
                    inst.origin = "registry"
                elif source and not source.startswith("builtin"):
                    inst.origin = "local"
                else:
                    inst.origin = "registry"
                inst.resources = "gateway"
                inst.lifecycle = "gateway"
            inst.schemaVersion = 2
        errors = inst.validate_fields()
        if errors:
            logger.warning(
                "InstalledApp %s has invalid fields: %s — using defaults",
                inst.name,
                errors,
            )
            if inst.origin not in _VALID_ORIGIN:
                inst.origin = "registry"
            if inst.resources not in _VALID_RESOURCES:
                inst.resources = "gateway"
            if inst.lifecycle not in _VALID_LIFECYCLE:
                inst.lifecycle = "gateway"
        return inst


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_installed(name: str) -> InstalledApp | None:
    """Read installed.json for an app, or None if not installed."""
    meta_path = app_dir(name) / INSTALLED_META_FILENAME
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return InstalledApp.from_dict(data)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", meta_path, exc)
        return None


def _write_installed(name: str, meta: InstalledApp) -> None:
    """Write installed.json for an app."""
    meta_path = app_dir(name) / INSTALLED_META_FILENAME
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(meta_path, json.dumps(meta.to_dict(), indent=2) + "\n")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class AppResult:
    """Result of an app lifecycle operation."""

    ok: bool = True
    name: str = ""
    message: str = ""
    error: str = ""
    error_code: str = ""  # structured error code for HTTP status mapping
    secret: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": self.ok, "name": self.name}
        if self.message:
            d["message"] = self.message
        if self.error:
            d["error"] = self.error
        # `code` is the repo's wire contract for a machine-readable failure
        # (test_error_code_contract.py); `error` is advisory prose. This field
        # existed but was never serialized, so every structured code set by a
        # caller was silently dropped on the way to the client -- leaving the
        # frontend with untranslatable English prose and no way to tell WHICH
        # failure it was, which is why an execution-policy denial could not be
        # given an actionable affordance.
        if self.error_code:
            d["code"] = self.error_code
        return d


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_source_path(source: Path) -> list[str]:
    """Validate that a source directory looks like a valid app."""
    errors: list[str] = []
    manifest_path = source / APP_MANIFEST_FILENAME
    if not manifest_path.is_file():
        errors.append(f"missing {APP_MANIFEST_FILENAME} in {source}")
        return errors
    try:
        manifest = AppManifest.from_json_file(manifest_path)
    except (json.JSONDecodeError, ValueError) as exc:
        errors.append(f"invalid {APP_MANIFEST_FILENAME}: {exc}")
        return errors
    errors.extend(manifest.validate(app_root=source))
    if manifest.minKiroCrewVersion:
        ver_err = _check_min_version(manifest.minKiroCrewVersion)
        if ver_err:
            errors.append(ver_err)
    return errors


def _check_min_version(min_version: str) -> str | None:
    """Return error string if current KiroCrew version is too old, else None."""
    return check_min_version(min_version)


def _check_path_safety(path: str) -> bool:
    """Return True if a resource path is safe (no traversal).

    Rejects ``..``, ``/``, and ``\\`` to prevent directory traversal
    when the path is used as a key in file-system lookups (e.g.
    ``apps_dir() / name``).
    """
    return ".." not in path and "/" not in path and "\\" not in path


# Build-input / VCS directories never needed at runtime.  The app-kit runtime
# layout is ``app.json`` + backend code + ``ui/dist/`` — ``node_modules`` is
# npm build input and ``.git`` comes from cloned registry sources.
# ``shutil.ignore_patterns`` matches by basename at every depth, so both
# ``node_modules`` and ``ui/node_modules`` are dropped.  ``build`` is
# deliberately NOT listed: the manifest may reference runtime paths anywhere
# under the app root, and silently dropping a manifest-referenced directory
# would record a successful install with missing files.  A ``build`` symlink
# into a huge build tree is already neutralized by ``symlinks=True``.
_COPY_IGNORE = ("node_modules", ".git", "__pycache__", ".venv")


def _copy_app_tree(source: Path, dest: Path) -> None:
    """Copy an app source tree for install/update.

    - Symlinks are never followed. A symlink whose resolved target stays
      inside ``source`` is preserved as a symlink (e.g. an in-tree relative
      link); a symlink resolving OUTSIDE the source root is omitted
      entirely.  This makes the historic failure mode (a ``build`` symlink
      into a multi-GB build tree walked on copy) structurally impossible,
      and it prevents a link like ``ui -> ~/.docker`` from either copying
      or later serving sensitive files through the app UI route (same
      intent as ``snapshot._copytree_safe``).
    - ``ignore``: drop build-input/VCS dirs never needed at runtime.

    Callers on the asyncio event loop must run this off-loop (executor /
    ``asyncio.to_thread``) — a large copy is blocking filesystem I/O.
    """
    src_root = os.path.realpath(source)
    # os.path.isjunction: Python 3.12+ (always False off-Windows). Windows
    # directory junctions are reparse points NOT reported by islink(), and
    # copytree would descend into them despite symlinks=True — omit them.
    _isjunction = getattr(os.path, "isjunction", None)

    def _ignore(dir_path: str, names: list[str]) -> set[str]:
        skip = {n for n in names if n in _COPY_IGNORE}
        for n in names:
            if n in skip:
                continue
            p = os.path.join(dir_path, n)
            if _isjunction is not None and _isjunction(p):
                # Junctions cannot be preserved as links by copytree; never
                # copy through one (it may point at a sensitive location).
                logger.warning("Omitting directory junction in app source: %s", p)
                skip.add(n)
                continue
            if os.path.islink(p):
                try:
                    target = os.path.realpath(p)
                    escapes = os.path.commonpath([src_root, target]) != src_root
                except ValueError:
                    # commonpath raises for paths on different drives
                    # (Windows) or mixed abs/rel — treat as escaping.
                    escapes = True
                if escapes:
                    logger.warning(
                        "Omitting symlink escaping app source root: %s", p
                    )
                    skip.add(n)
        return skip

    shutil.copytree(
        source,
        dest,
        dirs_exist_ok=True,
        symlinks=True,
        ignore=_ignore,
    )

    # Rewrite preserved ABSOLUTE in-tree symlinks to relative form: an
    # absolute link copied verbatim still points into the *source* tree, so
    # the installed copy would silently depend on (and break with) the local
    # source directory. Relative in-tree links are already correct as-is.
    for root, dirs, files in os.walk(dest):
        for n in dirs + files:
            p = os.path.join(root, n)
            if not os.path.islink(p):
                continue
            raw = os.readlink(p)
            if not os.path.isabs(raw):
                continue
            rel_to_src = os.path.relpath(os.path.realpath(p), src_root)
            os.remove(p)
            os.symlink(
                os.path.relpath(os.path.join(dest, rel_to_src), os.path.dirname(p)), p
            )


# Per-app lifecycle locks, shared by every async entry point (registry
# install, dashboard install/update/uninstall routes).  Once the blocking
# copy runs off-loop, two concurrent operations on the same app could
# otherwise race the installed-check against the copy — and update/uninstall
# use shared move-aside names (``.{name}-data-tmp``), so an interleaving can
# destroy preserved user data.  Different apps proceed in parallel.
_LIFECYCLE_LOCKS: dict[str, "asyncio.Lock"] = {}


def app_lifecycle_lock(name: str) -> "asyncio.Lock":
    """Return the per-app asyncio lock guarding install/update/uninstall.

    Must be called from (and the lock used on) the event loop thread; the
    guarded blocking work itself runs off-loop via executor/``to_thread``.
    """
    if name not in _LIFECYCLE_LOCKS:
        _LIFECYCLE_LOCKS[name] = asyncio.Lock()
    return _LIFECYCLE_LOCKS[name]


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install_app(source: str | Path) -> AppResult:
    """Install an app from a local directory path.

    1. Validate manifest
    2. Copy to ``~/.kiro/crew/apps/{name}/``
    3. Write ``installed.json``

    Resource registration (agents, skills, crons) is handled separately
    by the bridge module — this function only manages files.
    """
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"source={source!s}",
            error="source is not a directory",
        )
        return AppResult(ok=False, error=f"source is not a directory: {source}")

    errors = _validate_source_path(source)
    if errors:
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"source={source!s}",
            error="; ".join(errors),
        )
        return AppResult(ok=False, error="; ".join(errors))

    manifest = AppManifest.from_json_file(source / APP_MANIFEST_FILENAME)
    name = manifest.name
    dest = app_dir(name)

    # Guard against path traversal in manifest name
    if not _check_path_safety(name):
        sel().log_api_access(
            caller="app_install",
            operation="path_safety_check",
            outcome="rejected",
            resources=f"name={name!r}",
            error="unsafe app name (path traversal attempt)",
        )
        return AppResult(ok=False, name=name, error=f"unsafe app name: {name!r}")

    # Admission: the app allowlist/ban/signature gate INSTALL, not just
    # activation, so a banned / non-allowlisted app never lands on disk.
    denied = app_admission_denied(name, manifest=manifest, action="install")
    if denied:
        sel().log_api_access(
            caller="app_install",
            operation="admission",
            outcome="rejected",
            resources=f"name={name!r}",
            error=denied,
        )
        return AppResult(ok=False, name=name, error=f"blocked by admission policy: {denied}")

    # Check if already installed — reject, use update_app() or uninstall first
    existing = _read_installed(name)
    if existing:
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"name={name!r}",
            error=f"already installed (v{existing.version})",
        )
        return AppResult(
            ok=False,
            name=name,
            error=f"app {name!r} is already installed (v{existing.version}). "
            f"Uninstall first or use the update endpoint.",
        )

    # Preserve existing data/ directory (left behind by a prior default uninstall)
    existing_data = dest / "data" if dest.exists() else None
    # Use same temp name as uninstall_app/update_app so data stranded by a
    # crashed sibling operation is reclaimable by whichever lifecycle runs next.
    tmp_data = dest.parent / f".{name}-data-tmp"

    # Clean stale tmp from a previous failed install/uninstall.
    # Only remove tmp_data if the original data/ also exists (proving tmp is
    # truly stale). If data/ is gone, tmp_data may be the sole surviving copy.
    try:
        if tmp_data.is_dir():
            if existing_data and existing_data.is_dir():
                shutil.rmtree(str(tmp_data))
    except OSError as cleanup_exc:
        logger.error(
            "Failed to clean stale temp dir %s for app %s: %s",
            tmp_data,
            name,
            cleanup_exc,
        )
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"name={name!r}",
            error=f"stale temp cleanup: {cleanup_exc}",
        )
        return AppResult(
            ok=False,
            name=name,
            error=f"cannot clean stale temp dir {tmp_data}: {cleanup_exc}",
        )

    try:
        if existing_data and existing_data.is_dir():
            shutil.move(str(existing_data), str(tmp_data))
        elif tmp_data.is_dir():
            # tmp_data is the sole surviving copy from a prior crash —
            # keep it intact; it will be restored after copytree.
            pass

        if dest.exists():
            # No installed metadata for this app (checked above), yet the
            # dest dir exists — an orphaned partial copy from a prior crash
            # (e.g. hard kill mid-install). Remove and re-copy fresh.
            logger.warning("Removing orphaned partial install at %s", dest)
            shutil.rmtree(dest)
        _copy_app_tree(source, dest)

        # Restore preserved data/ (overwrite empty data/ from source package)
        if tmp_data.is_dir():
            restored = dest / "data"
            if restored.exists():
                shutil.rmtree(restored)
            shutil.move(str(tmp_data), str(restored))
    except (OSError, shutil.Error, ValueError) as exc:
        # Clean up partial install first
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        # Restore preserved data to the clean dest
        try:
            if tmp_data.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tmp_data), str(dest / "data"))
        except OSError as restore_exc:
            logger.error(
                "Failed to restore preserved data for app %s; " "data left at %s: %s",
                name,
                tmp_data,
                restore_exc,
            )
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"name={name!r}",
            error=f"copy failed: {exc}",
        )
        return AppResult(ok=False, name=name, error=f"failed to copy app files: {exc}")

    # Write installed metadata
    meta = InstalledApp(
        name=name,
        version=manifest.version,
        displayName=manifest.displayName,
        enabled=False,  # installed but not enabled until explicitly enabled
        installedAt=_now_iso(),
        source=str(source),
    )
    _write_installed(name, meta)

    # Create data directory
    app_data_dir(name)

    # Generate and write app secret for token-based auth (App Kit §5.1)
    # circular import: token_auth -> dashboard -> bridges -> manager
    from kiro_crew.dashboard.token_auth import generate_app_secret, write_app_secret

    write_app_secret(name, generate_app_secret())

    # Audit successful install for all callers (CLI, registry, dashboard)
    sel().log_api_access(
        caller="app_install",
        operation="install",
        outcome="success",
        resources=f"name={name!r} version={manifest.version}",
    )

    logger.info("Installed app %s v%s from %s", name, manifest.version, source)
    return AppResult(ok=True, name=name, message=f"installed {name} v{manifest.version}")


# ---------------------------------------------------------------------------
# Update (re-install in place)
# ---------------------------------------------------------------------------


def update_app(source: str | Path, *, expected_name: str | None = None) -> AppResult:
    """Update an already-installed app from a local directory path.

    1. Validate new manifest
    2. Preserve ``data/`` directory
    3. Replace app files
    4. Update ``installed.json``

    ``expected_name``: when given, reject the update unless the source
    manifest's ``name`` matches — callers that lock/route by app name must
    not let a mismatched source mutate a different app.
    """
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        return AppResult(ok=False, error=f"source is not a directory: {source}")

    errors = _validate_source_path(source)
    if errors:
        return AppResult(ok=False, error="; ".join(errors))

    manifest = AppManifest.from_json_file(source / APP_MANIFEST_FILENAME)
    name = manifest.name
    if expected_name is not None and name != expected_name:
        return AppResult(
            ok=False,
            name=expected_name,
            error=f"source manifest name {name!r} does not match app {expected_name!r}",
        )
    dest = app_dir(name)

    # Guard against path traversal in manifest name
    if not _check_path_safety(name):
        return AppResult(ok=False, error=f"unsafe app name: {name!r}")

    # Admission: re-gate on update so a policy that tightens after install
    # (e.g. an app is later banned) blocks a subsequent update in place.
    denied = app_admission_denied(name, manifest=manifest, action="update")
    if denied:
        sel().log_api_access(
            caller="app_update",
            operation="admission",
            outcome="rejected",
            resources=f"name={name!r}",
            error=denied,
        )
        return AppResult(ok=False, name=name, error=f"blocked by admission policy: {denied}")

    existing = _read_installed(name)
    if not existing:
        return AppResult(ok=False, name=name, error=f"app {name!r} is not installed")

    old_version = existing.version

    # Preserve data directory and app secret
    data_dir = dest / "data"
    secret_file = dest / ".app_secret"
    tmp_data = dest.parent / f".{name}-data-tmp"
    tmp_secret = dest.parent / f".{name}-secret-tmp"

    # Clean up stale tmp files from a previous failed update
    if tmp_data.is_dir() and data_dir.is_dir():
        shutil.rmtree(str(tmp_data))
    if tmp_secret.is_file() and secret_file.is_file():
        tmp_secret.unlink()

    try:
        if data_dir.is_dir():
            shutil.move(str(data_dir), str(tmp_data))
        if secret_file.is_file():
            shutil.move(str(secret_file), str(tmp_secret))

        # Replace app files
        shutil.rmtree(dest)
        _copy_app_tree(source, dest)

        # Restore data
        if tmp_data.is_dir():
            restored = dest / "data"
            if restored.exists():
                shutil.rmtree(restored)
            shutil.move(str(tmp_data), str(restored))
        # Restore secret
        if tmp_secret.is_file():
            shutil.move(str(tmp_secret), str(dest / ".app_secret"))
    except (OSError, shutil.Error, ValueError) as exc:
        # Attempt to restore on failure — each step independently wrapped
        try:
            if tmp_data.is_dir() and not data_dir.is_dir():
                shutil.move(str(tmp_data), str(data_dir))
        except OSError:
            pass
        try:
            if tmp_secret.is_file() and not secret_file.is_file():
                shutil.move(str(tmp_secret), str(secret_file))
        except OSError:
            pass
        return AppResult(ok=False, name=name, error=f"failed to update app files: {exc}")

    # Update metadata — carry every persisted field forward from ``existing``
    # via dataclasses.replace, overriding only what the update actually changes
    # (version/displayName/updatedAt/source). Constructing a fresh InstalledApp
    # here silently dropped any field not re-listed (enabled, installedAt,
    # origin, resources, lifecycle, schemaVersion, migratedTo, and — the bug
    # that surfaced this — the ``dev`` flag, so updating an app being iterated
    # on in dev mode wrote ``dev: false`` and later dropped it from live
    # reload). ``replace`` makes new fields regression-proof by construction.
    meta = replace(
        existing,
        version=manifest.version,
        displayName=manifest.displayName,
        updatedAt=_now_iso(),
        source=str(source),
    )
    _write_installed(name, meta)

    # Ensure data directory exists
    app_data_dir(name)

    logger.info(
        "Updated app %s: v%s -> v%s from %s",
        name,
        old_version,
        manifest.version,
        source,
    )
    return AppResult(
        ok=True,
        name=name,
        message=f"updated {name} v{old_version} -> v{manifest.version}",
    )


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def uninstall_app(name: str, *, keep_data: bool = True) -> AppResult:
    """Uninstall an app while preserving its ``data/`` directory by default.

    Passing ``keep_data=False`` is the explicit purge action. Resource
    deregistration should be done before calling this.
    Built-in apps cannot be uninstalled — only disabled.
    """
    if not _check_path_safety(name):
        return AppResult(ok=False, name=name, error=f"unsafe app name: {name!r}")
    meta = _read_installed(name)
    if not meta:
        return AppResult(ok=False, name=name, error=f"app {name!r} is not installed")
    if meta.lifecycle == "locked":
        return AppResult(
            ok=False,
            name=name,
            error=f"app {name!r} cannot be uninstalled (lifecycle=locked) — use disable instead",
        )
    dest = app_dir(name)
    if not dest.is_dir():
        return AppResult(ok=False, name=name, error=f"app {name!r} is not installed")

    try:
        if keep_data:
            data = dest / "data"
            # Move data to temp, remove app dir, move data back
            tmp_data = dest.parent / f".{name}-data-tmp"
            if data.is_dir():
                shutil.move(str(data), str(tmp_data))
            shutil.rmtree(dest)
            if tmp_data.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tmp_data), str(data))
        else:
            shutil.rmtree(dest)
    except OSError as exc:
        return AppResult(ok=False, name=name, error=f"failed to remove app: {exc}")

    logger.info("Uninstalled app %s (keep_data=%s)", name, keep_data)
    # Drop any dev-mode sentinel entry so an app later reinstalled under this
    # name does not inherit stale dev-mode serving/watching. Lazy import avoids
    # a module-level cycle (dev_mode imports from manager).
    try:
        from kiro_crew.apps.dev_mode import remove_dev_app

        remove_dev_app(name)
    except Exception:
        logger.debug("dev-mode cleanup on uninstall of %r failed", name, exc_info=True)
    return AppResult(ok=True, name=name, message=f"uninstalled {name}")


# ---------------------------------------------------------------------------
# Enable / Disable
# ---------------------------------------------------------------------------


def _app_activation_denied(name: str) -> str | None:
    """Return a denial reason if governance forbids activating app *name*, else None.

    The ``apps`` scope (a ScopedRuleset over app slugs) is the per-app activation
    allowlist: an enterprise policy may restrict which apps may run at all (e.g.
    ``apps: {mode: allow, allow: ["auto-research", "file-explorer"]}``).  Enabling
    is the activation chokepoint — a disabled app contributes no agents, skills,
    crons, or routes — so the gate lives here.  Resolution uses the ``_host``
    session key (surface ``host``): app activation is an operator/host action, so
    it is governed by the policy ceiling AND any ``bind: {type: surface, id:
    host}`` profile — an honest, stable bind target.  (It must NOT use an empty
    key, which would classify to surface ``unknown`` and silently match nothing;
    an empty key previously mis-classified to ``slack`` and accidentally picked up
    slack-bound profiles.)  Best-effort beyond the always-on
    checks: a ``PlatformCompositionError`` propagates (fail-closed CPP); any other
    error degrades to "no opinion" (None).
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import (
            HOST_SESSION_KEY,
            governance_permits,
        )

        decision = governance_permits("apps", name, session_key=HOST_SESSION_KEY)
        if not getattr(decision, "permitted", True):
            try:
                from kiro_crew.sel import sel

                sel().log_governance_decision(
                    session_key=HOST_SESSION_KEY, tool_name=f"enable_app:{name}", scope="apps",
                    item=name, outcome="denied",
                    rule=getattr(decision, "rule", ""), layer=getattr(decision, "layer", ""),
                    reason=getattr(decision, "reason", ""),
                )
            except Exception:
                logger.debug("app activation deny audit failed", exc_info=True)
            return getattr(decision, "reason", f"app {name!r} not permitted by policy")
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # scope="apps" + app=name so the SEL records WHICH app's activation gate
        # degraded; session_key=_host so the SEL source is the honest "host"
        # surface (not "unknown"/"slack").  Wrapped so a late-import failure cannot
        # raise out of this except-branch and convert the soft fail-open into a
        # hard fail.
        try:
            from kiro_crew.platform.governance_profiles import (
                HOST_SESSION_KEY,
                audit_governance_degraded,
            )

            audit_governance_degraded(
                "app_activation", session_key=HOST_SESSION_KEY, scope="apps", app=name
            )
        except Exception:
            logger.debug("governance degrade audit unavailable", exc_info=True)
        return None


def enable_app(name: str) -> AppResult:
    """Enable an installed app."""
    if not _check_path_safety(name):
        return AppResult(ok=False, name=name, error=f"unsafe app name: {name!r}")
    meta = _read_installed(name)
    if not meta:
        return AppResult(ok=False, name=name, error=f"app {name!r} is not installed")
    # Governance: the ``apps`` allowlist may forbid activating this app entirely.
    gov_denied = _app_activation_denied(name)
    if gov_denied:
        return AppResult(ok=False, name=name, error=f"blocked by governance policy: {gov_denied}")

    # Admission: the ban/allowlist also gates activation so a policy that bans
    # an already-installed app blocks it from being (re-)enabled. Builtins
    # (origin == "builtin") are trusted first-party code shipped unsigned with
    # defaultEnabled=False, so a require_signature / non-empty allowlist policy
    # would otherwise make every core app permanently un-enableable. The gate
    # governs third-party install/enable, not first-party code — exempt builtins.
    if meta.origin != "builtin":
        denied = app_admission_denied(name, manifest=get_app_manifest(name), action="enable")
        if denied:
            sel().log_api_access(
                caller="app_enable",
                operation="admission",
                outcome="rejected",
                resources=f"name={name!r}",
                error=denied,
            )
            return AppResult(
                ok=False, name=name, error=f"blocked by admission policy: {denied}"
            )

    # Deny before enabled metadata or any route-level registration, dependency,
    # lifecycle-script, hook, or backend side effect can occur.
    execution_denied = app_execution_denied(
        name,
        action="enable",
        app_root=shipped_builtin_app_root(name),
        caller="app_enable",
    )
    if execution_denied:
        return AppResult(
            ok=False,
            name=name,
            error=f"blocked by execution policy: {execution_denied}",
            error_code="app_execution_denied",
        )

    if meta.enabled:
        return AppResult(ok=True, name=name, message=f"{name} is already enabled")

    meta.enabled = True
    meta.updatedAt = _now_iso()
    _write_installed(name, meta)

    logger.info("Enabled app %s", name)
    return AppResult(ok=True, name=name, message=f"enabled {name}")


def disable_app(name: str) -> AppResult:
    """Disable an installed app without removing it."""
    if not _check_path_safety(name):
        return AppResult(ok=False, name=name, error=f"unsafe app name: {name!r}")
    meta = _read_installed(name)
    if not meta:
        return AppResult(ok=False, name=name, error=f"app {name!r} is not installed")
    if not meta.enabled:
        return AppResult(ok=True, name=name, message=f"{name} is already disabled")

    meta.enabled = False
    meta.updatedAt = _now_iso()
    _write_installed(name, meta)

    logger.info("Disabled app %s", name)
    return AppResult(ok=True, name=name, message=f"disabled {name}")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_apps() -> list[dict[str, Any]]:
    """Return metadata for all installed apps."""
    root = apps_dir()
    if not root.is_dir():
        return []
    orphaned_set = detect_orphaned_builtins()
    result: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        meta = _read_installed(entry.name)
        if not meta:
            continue
        # Also load manifest for full info
        manifest_path = entry / APP_MANIFEST_FILENAME
        manifest_data: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                manifest = AppManifest.from_json_file(manifest_path)
                manifest_data = manifest.to_dict()
                # For self-managed apps, the app may update its own
                # app.json without going through update_app().  Sync
                # the version from the manifest so the dashboard shows
                # the real version instead of a stale installed.json.
                if (
                    meta.lifecycle == "app"
                    and manifest.version
                    and manifest.version != meta.version
                ):
                    logger.debug(
                        "Syncing %s version: installed=%s manifest=%s",
                        meta.name,
                        meta.version,
                        manifest.version,
                    )
                    meta.version = manifest.version
                    meta.updatedAt = _now_iso()
                    _write_installed(entry.name, meta)
            except Exception:
                pass
        app_info: dict[str, Any] = {
            **meta.to_dict(),
            "manifest": manifest_data,
        }
        # Include migratedTo if non-empty
        if meta.migratedTo:
            app_info["migratedTo"] = meta.migratedTo
        # Mark orphaned builtins
        if entry.name in orphaned_set:
            app_info["orphaned"] = True
        result.append(app_info)
    return result


def get_app(name: str) -> dict[str, Any] | None:
    """Return full metadata for a single installed app, or None."""
    meta = _read_installed(name)
    if not meta:
        return None
    manifest_path = app_dir(name) / APP_MANIFEST_FILENAME
    manifest_data: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = AppManifest.from_json_file(manifest_path)
            manifest_data = manifest.to_dict()
            # Sync version for self-managed apps (same as list_apps)
            if meta.lifecycle == "app" and manifest.version and manifest.version != meta.version:
                meta.version = manifest.version
                meta.updatedAt = _now_iso()
                _write_installed(name, meta)
        except Exception:
            pass
    return {**meta.to_dict(), "manifest": manifest_data}


def get_app_manifest(name: str) -> AppManifest | None:
    """Return the parsed manifest for an installed app, or None."""
    manifest_path = app_dir(name) / APP_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        return AppManifest.from_json_file(manifest_path)
    except Exception:
        return None


def is_app_enabled(name: str) -> bool:
    """Read-only enablement check: True only for an installed, enabled app.

    Unlike ``get_app`` this never writes (no version-sync side effect), so it
    is safe to call from worker threads (e.g. ``asyncio.to_thread``) without
    racing loop-side writers of ``installed.json``.
    """
    meta = _read_installed(name)
    return bool(meta and meta.enabled)


def set_app_source(name: str, source: str) -> bool:
    """Update the ``source`` field of an installed app's metadata.

    Returns True if the update succeeded, False if the app is not installed.
    Used by the registry module to mark apps as registry-installed after
    the temp clone directory is cleaned up.
    """
    meta = _read_installed(name)
    if not meta:
        return False
    meta.source = source
    _write_installed(name, meta)
    return True


def set_app_provenance(
    name: str,
    *,
    source: str,
    url: str,
    registry: str = "",
    commit: str = "",
    signer: str = "",
) -> bool:
    """Record the full install provenance of a registry-installed app.

    Superset of :func:`set_app_source`: alongside the bare ``registry:<name>``
    marker it persists WHICH source the app actually came from (*url* plus the
    originating external *registry* id, empty for the bundled catalog), the
    *commit* resolved in that source clone, and the verified *signer* if the
    admission layer verified one.  Updates resolve from these fields instead of
    re-looking-up the bare name, so a same-named entry published by a different
    registry source cannot capture an installed app's updates.

    Uses ``dataclasses.replace`` so every other persisted field (``enabled``,
    ``dev``, ``origin``, ...) carries forward untouched.

    Returns True if the update succeeded, False if the app is not installed.
    """
    meta = _read_installed(name)
    if not meta:
        return False
    _write_installed(
        name,
        replace(
            meta,
            source=source,
            sourceUrl=url,
            sourceRegistry=registry,
            sourceCommit=commit,
            sourceSigner=signer,
        ),
    )
    return True


# ---------------------------------------------------------------------------
# External (self-managed) app registration
# ---------------------------------------------------------------------------


def register_external_app(
    name: str,
    version: str,
    display_name: str,
    *,
    source: str = "",
    manifest_data: dict[str, Any] | None = None,
    origin: str = "external",
    resources: str = "app",
    lifecycle: str = "app",
) -> AppResult:
    """Register a self-managed app with KiroCrew's app system.

    Self-managed apps (``resources="app"``) handle their own agent/skill/MCP
    registration.  KiroCrew only tracks metadata so the dashboard can display them.

    If the app is already registered, updates version and manifest.

    Args:
        name: App identifier (kebab-case).
        version: Semver version string.
        display_name: Human-readable name.
        source: Where the app was installed from (path, URL, etc.).
        manifest_data: Optional full app.json content to persist.
        origin: Classification — where the app came from.
        resources: Classification — who manages resource registration.
        lifecycle: Classification — who manages updates/uninstall.

    Returns:
        AppResult indicating success or failure.
    """
    if not _check_path_safety(name):
        return AppResult(ok=False, error=f"unsafe app name: {name!r}")

    # Enforce the canonical lowercase-ASCII kebab-case form on the
    # self-registration path (CWE-178). Admission normalizes with
    # NFKC+casefold+strip, but the backend below stores/resolves the app by the
    # RAW name (app_dir(name), _write_installed(name), write_app_secret(name)),
    # so without this an admitted "Safe-App"/"safe-app "/Unicode-equivalent
    # would diverge from the approved identity. install_app/update_app already
    # gate on KEBAB_RE via AppManifest; this closes the register_external gap.
    from kiro_crew.apps.manifest import KEBAB_RE

    if not KEBAB_RE.match(name):
        return AppResult(
            ok=False, name=name,
            error=f"invalid app name (must be lowercase kebab-case): {name!r}",
        )

    # Builtin provenance is assigned only by register_builtin_apps(). Accepting
    # it from self-registration would make the execution exemption caller-controlled.
    if origin == "builtin":
        sel().log_api_access(
            caller="app_register_external",
            operation="provenance",
            outcome="rejected",
            resources=f"name={name!r} origin=builtin",
            error="builtin origin is reserved",
        )
        return AppResult(
            ok=False,
            name=name,
            error="builtin origin is reserved for KiroCrew-shipped apps",
        )

    # Admission: register_external_app writes enabled=True and is HTTP-reachable
    # (POST /api/apps/register), so it is an install+enable path and MUST be
    # gated too — otherwise a banned/non-allowlisted app can self-register and
    # activate with no admission control. Pass the self-reported manifest (when
    # provided) so a correctly-signed app is admitted under require_signature.
    admission_manifest = None
    if manifest_data:
        admission_manifest = AppManifest.from_dict(manifest_data)
    denied = app_admission_denied(
        name, manifest=admission_manifest, action="register_external"
    )
    if denied:
        sel().log_api_access(
            caller="app_register_external",
            operation="admission",
            outcome="rejected",
            resources=f"name={name!r}",
            error=denied,
        )
        return AppResult(ok=False, name=name, error=f"blocked by admission policy: {denied}")

    dest = app_dir(name)
    existing = _read_installed(name)

    # Builtin provenance is assigned ONLY by register_builtin_apps(). A
    # self-registration must never OVERWRITE an existing builtin-owned record
    # (which the update branch below would do — downgrading origin/lifecycle to
    # external/app). That would both hand a third-party app a shipped builtin's
    # execution exemption AND leave the boot-warmed first-party name / MCP-server
    # sets stale until the next gateway restart. Stand down, mirroring
    # register_builtin_apps()'s refusal to take over a user-installed app — so a
    # builtin's provenance is immutable at runtime and the warmed sets stay valid.
    if existing and _builtin_owns_install(existing):
        sel().log_api_access(
            caller="app_register_external",
            operation="provenance",
            outcome="rejected",
            resources=f"name={name!r}",
            error="builtin-owned app cannot be replaced by self-registration",
        )
        return AppResult(
            ok=False,
            name=name,
            error=(
                f"{name!r} is a KiroCrew-shipped builtin and cannot be replaced "
                "by self-registration"
            ),
        )

    if existing:
        # Update existing registration
        existing.version = version
        existing.displayName = display_name
        existing.updatedAt = _now_iso()
        if source:
            existing.source = source
        existing.origin = origin
        existing.resources = resources
        existing.lifecycle = lifecycle
        _write_installed(name, existing)
    else:
        # New registration
        dest.mkdir(parents=True, exist_ok=True)
        meta = InstalledApp(
            name=name,
            version=version,
            displayName=display_name,
            enabled=True,  # self-managed apps are always "enabled"
            installedAt=_now_iso(),
            source=source,
            origin=origin,
            resources=resources,
            lifecycle=lifecycle,
        )
        _write_installed(name, meta)

    # Persist manifest if provided (so dashboard can show full info)
    if manifest_data:
        manifest_path = dest / APP_MANIFEST_FILENAME
        atomic_write(manifest_path, json.dumps(manifest_data, indent=2) + "\n")

    # Ensure data directory exists
    app_data_dir(name)

    # Generate app secret only for new registrations — preserve existing secrets
    from kiro_crew.dashboard.token_auth import generate_app_secret, write_app_secret

    secret_path = dest / ".app_secret"
    is_new_secret = not (existing and secret_path.is_file())
    if is_new_secret:
        secret = generate_app_secret()
        write_app_secret(name, secret)
    else:
        secret = ""

    action = "updated" if existing else "registered"
    logger.info(
        "External app %s %s: v%s (origin=%s, resources=%s, lifecycle=%s)",
        name,
        action,
        version,
        origin,
        resources,
        lifecycle,
    )
    result = AppResult(
        ok=True,
        name=name,
        message=f"{action} {name} v{version}",
        secret=secret if is_new_secret else "",
    )
    return result


# ---------------------------------------------------------------------------
# Built-in app registration
# ---------------------------------------------------------------------------

# Built-in apps are features baked into the KiroCrew dashboard that we
# surface in the App Store as "builtin" entries.  They use the host's
# React tree directly (no ESM bundle) and their page components resolve
# through ``BUILTIN_COMPONENT_REGISTRY`` in the frontend.  The registration
# here is metadata-only so the App Store can display them alongside
# installable apps.
#
# Default-disabled policy: a builtin app ships with ``defaultEnabled: False``
# so a fresh install presents a minimal sidebar (core surfaces only) instead of
# every app at once. Apps are opt-in from the App Store Browse tab. Because
# ``register_builtin_apps()`` applies ``defaultEnabled`` only on first
# registration and preserves user state on restart, existing users keep
# whatever they already enabled — this only changes the out-of-the-box
# experience for new installs.
#
# The exception is _DEFAULT_ON_BUILTINS below.

# Builtins deliberately shipped ENABLED on a fresh install, exempt from the
# opt-in policy above because they are core surfaces rather than optional
# add-ons. Adding a name here is a product decision, not a convenience — keep
# the set small. A default-on builtin still honors the ``apps`` governance
# allowlist at registration (see _app_activation_denied), so a deny-by-default
# host policy is never bypassed.
#
# This is the single source of truth for the exemption: the policy tests over
# both the hardcoded list and the file-based manifests read it from here, so a
# builtin cannot become default-on in one registration path while the other
# path's test still forbids it.
_DEFAULT_ON_BUILTINS: frozenset[str] = frozenset({"projects"})  # Task Runner

# EMPTY, and that is a finished migration rather than an oversight. Every builtin now
# ships as a file-based manifest under ``builtins/<dir>/app.json`` and is picked up by
# ``discover_builtin_apps()``. ``agent-worlds`` and ``channels`` were the last two
# hardcoded entries; they moved to ``builtins/agent_worlds/app.json`` and
# ``builtins/channels/app.json`` with every field byte-identical, including the
# ``defaultEnabled: false`` / ``hidden: true`` flags, which survive because
# ``_manifest_to_builtin_dict`` copies ``AppManifest.extra`` verbatim.
#
# One thing JSON cannot carry came with them, so it is recorded here: ``channels`` sets
# ``hidden: true`` to keep itself out of the App Store Browse grid only. Its code and
# routes stay fully intact and it is enabled with ``kirocrew app enable channels``.
# ``hidden`` gates store visibility, nothing else.
#
# Why it had to happen for i18n: the display copy of a builtin is localised by the
# ``APP_MANIFEST_KEY`` table in ``website/src/components/appstore/appManifest.ts``, and
# ``scripts/check-app-manifest-sync.mjs`` proves the English catalog value still equals
# the manifest's own prose. A manifest that lives in a Python literal has no file for
# that check to read, so these two apps would have been the only builtins whose copy
# could drift silently.
#
# It stays a list rather than being deleted because it is still the ADD-only precedence
# seam: ``register_builtin_apps`` and ``detect_orphaned_builtins`` union it with the
# discovered and edition-contributed sets, so an edition (or a test) can inject a
# builtin that outranks a discovered one without reintroducing the hardcoding. Prefer a
# file manifest; reach for this only when there is no directory to put one in.
_BUILTIN_APPS: list[dict[str, Any]] = []


_REQUIRED_BUILTIN_FIELDS = {"name", "version", "displayName", "description", "author"}


def _validate_builtin_app(app_data: dict[str, Any]) -> list[str]:
    """Validate a builtin app definition. Returns list of errors (empty = valid).

    Builtin App Definition Schema:

    Required fields:
      - name (str): Kebab-case app identifier (e.g. "my-feature")
      - version (str): Semver version string (e.g. "1.0.0")
      - displayName (str): Human-readable name shown in App Store
      - description (str): Short description for App Store listing
      - author (str): Author name or team

    Optional fields:
      - tags (list[str]): Categorization tags for discovery
      - defaultEnabled (bool): Initial enabled state on first registration.
          Default: True. Set to False for apps that should be opt-in.
      - permissions (dict): API and event permissions declaration
      - ui (dict): UI configuration with "pages" list for sidebar entries
          Each page: {"route": str, "label": str, "icon": str}
    """
    errors: list[str] = []
    for field in _REQUIRED_BUILTIN_FIELDS:
        if not app_data.get(field):
            errors.append(f"missing required field: {field}")
    if "defaultEnabled" in app_data and not isinstance(app_data["defaultEnabled"], bool):
        errors.append("defaultEnabled must be a boolean")
    name = app_data.get("name", "")
    if name and not _check_path_safety(name):
        errors.append(f"unsafe app name: {name!r}")
    # migratedTo validation is lenient — invalid formats are handled by
    # _effective_migrated_to() which returns "" for bad values.  We log a
    # warning in register_builtin_apps() but do NOT block registration.
    # See design doc: "Log warning, skip the migratedTo field (app still
    # registers normally)".
    return errors


def _effective_migrated_to(app_data: dict[str, Any]) -> str:
    """Return migratedTo value if valid format, else empty string.

    Pure helper — does not mutate app_data.
    """
    migrated_to = app_data.get("migratedTo", "")
    if migrated_to and not re.match(
        r"^(registry|standalone):[a-z][a-z0-9]*(-[a-z0-9]+)*$", migrated_to
    ):
        return ""
    return migrated_to


def _edition_builtin_apps() -> list[dict[str, Any]]:
    """Builtin apps contributed by the active PlatformContext's AppsLoader.

    The Default ``AppsLoader`` returns empty ``manifest_sources`` so the
    standalone discovery set is exactly the package's ``builtins/`` dir — no
    extra apps, byte-for-byte today's behavior.  The internal companion returns a
    directory (inside the companion package) holding the feature-app
    ``app.json`` manifests; each such dir is scanned with the SAME
    ``discover_builtin_apps`` logic (subdir-with-app.json → app dict), so the
    companion's apps are namespaced/validated/registered identically to the
    OSS builtins.  Missing dirs are skipped gracefully by ``discover_builtin_apps``.
    """
    # Fail-closed via safe_context_call: a non-standalone host that cannot compose
    # re-raises PlatformCompositionError (never silently degrades to the OSS builtin
    # set); any other lookup failure falls back to no edition sources.
    _no_sources: list[Path] = []
    sources = safe_context_call(
        lambda: current_context().apps_loader.manifest_sources(),
        fallback=_no_sources,
        log_message="apps_loader.manifest_sources lookup failed; using none",
    )

    apps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        # discover_builtin_apps already skips a non-existent dir and validates
        # each manifest, so a bad/missing source can never break registration.
        for app_data in discover_builtin_apps(Path(source)):
            name = app_data.get("name", "")
            if name and name not in seen:
                seen.add(name)
                apps.append(app_data)
    return apps


def _edition_bundled_app_names() -> list[str]:
    """Names the active edition declares it bundles (PlatformContext).

    The Default ``AppsLoader`` returns the OSS builtins (``auto_research`` /
    ``file_explorer``) which are already covered by the package's ``builtins/``
    discovery, so this is a no-op for standalone.  The internal companion declares
    its feature-app names; used by orphan detection so a declared app is never
    mis-orphaned even if its manifest dir is momentarily unavailable.
    """
    # Fail-closed via safe_context_call (see _edition_builtin_apps above).
    _no_names: list[str] = []
    return list(
        safe_context_call(
            lambda: current_context().apps_loader.bundled_app_names(),
            fallback=_no_names,
            log_message="apps_loader.bundled_app_names lookup failed; using none",
        )
    )


def _rmtree_dirfd(fd: int) -> None:
    """Recursively delete the contents of an OPEN directory descriptor using
    only dir_fd-relative operations — immune to rename/symlink swaps because
    no absolute path is ever re-resolved."""
    with os.scandir(fd) as it:
        entries = list(it)
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            child = os.open(
                entry.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_DIRECTORY,
                dir_fd=fd,
            )
            try:
                _rmtree_dirfd(child)
            finally:
                os.close(child)
            os.rmdir(entry.name, dir_fd=fd)
        else:
            os.unlink(entry.name, dir_fd=fd)


def _dirfd_ops_supported() -> bool:
    return (
        os.open in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
    )


def resolve_mcp_backend_url(mcp_servers: Any) -> str | None:
    """Derive an app backend's base URL from its ``mcpServers`` declaration.

    This is the single definition of that rule.  Self-managed apps -- ones the
    gateway does not spawn, like the Crew Companion desktop app on :7778 --
    declare no ``backend.entryPoint``, so their backend is discovered from the
    MCP URL instead, with the path stripped.

    TWO callers depend on agreeing exactly, which is why this is one function
    and not two copies: ``handle_app_api_proxy`` resolves the URL to forward to,
    and ``register_builtin_apps`` decides whether to write the ``.app_secret``
    the proxy signs with.  If they ever disagree, an app resolves a backend and
    is then refused a secret, and every proxied request fails with 502 "has no
    secret" -- silently, since nothing checks at registration time.

    Returns None when no usable URL is declared.  Refused, matching the proxy's
    own guards: a non-loopback host (SSRF via a manifest-declared URL), a
    non-literal host (parsed with ``ip_address``, so a DNS name never resolves
    here), and the gateway's own port (self-referential, not a real backend).
    """
    if not isinstance(mcp_servers, dict):
        return None
    gateway_port = int(os.environ.get("KIROCREW_PORT", "5476"))
    for server_cfg in mcp_servers.values():
        if not isinstance(server_cfg, dict):
            continue
        url = server_cfg.get("url", "")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        # ONE guard around the whole parse. urlparse's accessors are lazy and
        # several raise ValueError on malformed input -- `parsed.port` does it for
        # "…:notaport". An escape from here propagates through
        # _app_declares_backend into register_builtin_apps() and the gateway fails
        # to START, so a single bad manifest would take down registration for every
        # builtin. A manifest is user-supplied data; it must only be skippable.
        try:
            parsed = urlparse(url)
            # Normalize localhost -> 127.0.0.1: aiohttp on macOS may fail on ::1.
            host = parsed.hostname or "127.0.0.1"
            if host == "localhost":
                host = "127.0.0.1"
            if not ipaddress.ip_address(host).is_loopback:
                logger.warning("Refusing non-loopback backend URL %s", url)
                continue
            port_num = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            # Non-IP host, unparsable port, or any other malformed component.
            logger.warning("Refusing unusable backend URL %s: %s", url, exc)
            continue
        if port_num == gateway_port:
            logger.warning("Refusing self-referential backend URL %s", url)
            continue
        return f"{parsed.scheme}://{host}:{port_num}"
    return None


def _builtin_owns_install(existing: InstalledApp) -> bool:
    """Whether an existing app entry was written by ``register_builtin_apps()``.

    False means a USER installed an app under this name, and the builtin must not
    touch it. That distinction cannot be recovered once lost: registration would
    overwrite ``origin`` and set ``lifecycle="locked"``, so afterwards nothing on
    disk shows the install was ever user-owned, and the user can no longer
    uninstall it.

    ``source`` is the discriminator: this function is the only writer of
    ``source="builtin"``, while ``install_app()`` records the install path or
    registry ref. ``origin`` is accepted as a secondary signal so entries written
    by older gateway versions are still recognised as ours.
    """
    return existing.source == "builtin" or existing.origin == "builtin"


def builtin_owns_installed(name: str) -> bool:
    """Whether the ACTIVE installed record for ``name`` is builtin-owned.

    ``True`` only when an ``installed.json`` exists for ``name`` AND it was
    written by :func:`register_builtin_apps` (``source``/``origin`` == builtin,
    per :func:`_builtin_owns_install`). A user-installed app that shadows a
    builtin's name — which makes registration *stand down* and leaves the
    user's record in place — or a missing/unreadable record both return
    ``False`` (fail-closed). Callers use this to confirm a shipped-manifest name
    is actually occupied by first-party code before granting it first-party
    trust; it can only REMOVE trust, never manufacture it.
    """
    existing = _read_installed(name)
    return existing is not None and _builtin_owns_install(existing)


# Apps this package now ships as a BUILTIN, but which users may already have
# installed as an EXTERNAL app under the same id.  Value = the upstream
# repository that external app is published from.
#
# Why an identity check and not just a name: ``register_builtin_apps()`` stands
# down whenever a name is occupied by a user-installed app, which is exactly
# right in the general case (that install may hold the user's own code) but
# means the very users being graduated would never receive the builtin.  The
# takeover therefore happens ONLY when the on-disk manifest names this exact
# repository, so an unrelated app that merely shares the id is still left alone.
#
# ``_escalated`` above cannot express this: it only removes directories whose
# ``installed.json`` says ``origin="builtin"``, and a user-installed external
# app is ``origin`` registry/local/external — so listing a graduated external
# app there is a no-op.
_SUPERSEDED_EXTERNALS: dict[str, str] = {
    "design-tweak": "https://github.com/michellemxm/kc-app-design-tweak",
}

_SUPERSEDED_ARCHIVE_DIRNAME = "apps-superseded"

# The takeover is a ONE-TIME migration, not a standing policy.  A receipt is
# written beside the archive the first time it runs and stands the takeover down
# forever after, so a user who deliberately REINSTALLS the external app
# afterwards keeps it (and the takeover cannot re-run on every gateway start).
#
# Why the receipt lives here and not in the app's own ``installed.json``:
# reinstalling the external app necessarily REPLACES ``apps/<name>/`` --
# ``install_app`` refuses while an ``installed.json`` is present, so the dir is
# removed first, and the install then writes a fresh ``installed.json`` from the
# source manifest.  A marker stored inside the app dir would therefore be
# destroyed by the exact act it has to survive.  ``apps-superseded/`` is state
# this feature already owns, outside ``apps/`` and never enumerated as an app,
# which makes it the durable home -- no new top-level file is introduced.
_SUPERSEDED_RECEIPT_SUFFIX = ".superseded.json"

# The ``-<UTC stamp>`` (plus optional collision suffix) that
# ``_archive_superseded_install`` appends to an archived install's dir name.
_SUPERSEDED_ARCHIVE_STAMP_RE = re.compile(r"-\d{8}T\d{6}(?:-\d+)?")


def _normalize_repo_url(url: str) -> str:
    """Canonical form for comparing two git remote spellings."""
    cleaned = url.strip().lower().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    return cleaned


def _compare_app_versions(left: str, right: str) -> int | None:
    """Compare two app version strings, or None when the answer is undecidable.

    Returns -1/0/1 like a classic comparator.  ``None`` means at least one side
    is not a parseable semver-ish string (``""``, ``"nightly"``, ``"1.x"``), and
    callers MUST treat that as "do not know" rather than as any ordering.
    """
    try:
        a, b = parse_version(left), parse_version(right)
    except (ValueError, TypeError, AttributeError):
        return None
    return (a > b) - (a < b)


def _superseded_receipt_path(name: str) -> Path:
    """Durable one-time marker recording that *name* was already superseded."""
    return config_dir() / _SUPERSEDED_ARCHIVE_DIRNAME / f"{name}{_SUPERSEDED_RECEIPT_SUFFIX}"


def _already_superseded(name: str) -> bool:
    """Whether the graduation takeover has already run once for *name*.

    The receipt is the record.  An archived install for the same name counts as
    a fallback marker so that a failed receipt WRITE (full disk, read-only home)
    cannot hand the user a second takeover — the archive is the proof it ran.
    """
    if _superseded_receipt_path(name).is_file():
        return True
    archive_root = config_dir() / _SUPERSEDED_ARCHIVE_DIRNAME
    try:
        entries = list(archive_root.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.startswith(name):
            continue
        # Only "<name>-<stamp>" is ours: a different app whose id merely starts
        # with this one ("design-tweak-pro-...") must not count as a marker.
        if _SUPERSEDED_ARCHIVE_STAMP_RE.fullmatch(entry.name[len(name):]) and entry.is_dir():
            return True
    return False


def _record_superseded(
    name: str, archive: Path, builtin_version: str, enabled: bool = False
) -> None:
    """Write the one-time receipt.  Non-fatal: the archive itself also marks it.

    ``enabled`` records whether the SUPERSEDED external app was enabled. The
    registration path normally carries that across in-process, but a takeover
    interrupted before the metadata write leaves the next boot with no installed
    record to read it from — so without it here, a recovery boot would silently
    re-register the app DISABLED (builtins default to ``defaultEnabled: false``)
    and the user would find a feature they had turned on switched off.
    """
    receipt = _superseded_receipt_path(name)
    try:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            receipt,
            json.dumps(
                {
                    "name": name,
                    "supersededAt": _now_iso(),
                    "archive": str(archive),
                    "builtinVersion": builtin_version,
                    "enabled": bool(enabled),
                    "note": (
                        "The bundled builtin took over this app id once. Reinstalling "
                        "the external app is honoured from here on: delete this file "
                        "only if you want the one-time takeover to be able to re-run."
                    ),
                },
                indent=2,
            )
            + "\n",
        )
    except OSError as exc:
        logger.warning(
            "Superseded %r but could not write the one-time receipt %s: %s",
            name,
            receipt,
            exc,
        )


def _superseding_external_install(name: str, builtin_version: str) -> bool:
    """Whether the app dir holds the external app this builtin graduates.

    Reads the manifest already on disk and requires its ``repository`` to be the
    one recorded in :data:`_SUPERSEDED_EXTERNALS`.  Any other app under this
    name -- or a manifest we cannot read -- answers False, so the caller stands
    down exactly as before.

    Two further refusals keep the takeover honest:

    * it has ALREADY run once on this host (see :func:`_already_superseded`), so
      a deliberate reinstall of the external app is the user's decision to keep;
    * the installed external app is NEWER than the bundled builtin, or the two
      versions cannot be compared at all -- taking over would be a downgrade,
      and an undecidable comparison resolves to leaving the user alone.
    """
    expected = _SUPERSEDED_EXTERNALS.get(name)
    if not expected:
        return False
    if _already_superseded(name):
        logger.info(
            "Not superseding %r: the one-time graduation already ran on this host "
            "(%s). This install is the user's own choice and is kept.",
            name,
            _superseded_receipt_path(name),
        )
        return False
    manifest_path = app_dir(name) / APP_MANIFEST_FILENAME
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.info(
            "Not superseding %r: cannot read its manifest to confirm identity: %s",
            name,
            exc,
        )
        return False
    repo = raw.get("repository") if isinstance(raw, dict) else None
    if not isinstance(repo, str) or _normalize_repo_url(repo) != _normalize_repo_url(expected):
        logger.info(
            "Not superseding %r: manifest repository %r is not the graduated app %r",
            name,
            repo,
            expected,
        )
        return False
    installed_version = raw.get("version") if isinstance(raw, dict) else None
    ordering = _compare_app_versions(
        installed_version if isinstance(installed_version, str) else "",
        builtin_version,
    )
    if ordering is None:
        logger.warning(
            "Not superseding %r: cannot compare the installed version %r with the "
            "bundled builtin version %r, so the user's install is left in place.",
            name,
            installed_version,
            builtin_version,
        )
        return False
    if ordering > 0:
        logger.warning(
            "Not superseding %r: the installed external app v%s is NEWER than the "
            "bundled builtin v%s — taking over would downgrade it. Leaving the "
            "user's install in place.",
            name,
            installed_version,
            builtin_version,
        )
        return False
    return True


def _finish_interrupted_supersede(name: str) -> bool:
    """Carry ``data/`` forward for a takeover that died between its two renames.

    :func:`_archive_superseded_install` does the graduation in two steps: rename
    the old install to the archive, then rename its ``data/`` into the fresh app
    dir.  Those cannot be one atomic operation (they are separate directories),
    so a gateway kill or power loss in between leaves the user's pending edit
    requests sitting in the archive while the builtin registers with no
    ``data/`` at all.

    That state does not heal itself, which is what makes it worth a recovery
    pass rather than a log line: the archive doubles as the "already superseded"
    fallback marker in :func:`_already_superseded`, so the takeover deliberately
    refuses to run a second time and would never come back to finish the move.

    Runs on every registration pass and is a **no-op** unless it finds that
    exact half-finished shape -- an archive for *name* still holding ``data/``
    while the live app dir has none:

    * a takeover that completed left no ``data/`` in the archive (it was renamed
      away), so there is nothing to do;
    * a user who deliberately REINSTALLED the external app afterwards has a live
      ``data/``, which is never touched or overwritten.

    Returns True only when data was actually carried forward.
    """
    if name not in _SUPERSEDED_EXTERNALS:
        return False
    archive_root = config_dir() / _SUPERSEDED_ARCHIVE_DIRNAME
    try:
        entries = sorted(archive_root.iterdir())
    except OSError:
        return False

    live = app_dir(name)
    live_data = live / "data"
    if live_data.is_dir() or live_data.is_symlink():
        return False  # the live install owns its data — never clobber it

    for entry in reversed(entries):  # newest stamp first
        if not entry.name.startswith(name):
            continue
        if not _SUPERSEDED_ARCHIVE_STAMP_RE.fullmatch(entry.name[len(name) :]):
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        stranded = entry / "data"
        if not (stranded.is_dir() or stranded.is_symlink()):
            continue
        try:
            live.mkdir(parents=True, exist_ok=True)
            os.rename(stranded, live_data)
        except OSError as exc:
            logger.warning(
                "Could not finish the interrupted takeover of %r: data/ is still "
                "in %s and was left there: %s",
                name,
                entry,
                exc,
            )
            return False
        logger.warning(
            "Finished an interrupted takeover of %r: carried data/ forward from %s "
            "into %s. Pending requests from the previous install are available again.",
            name,
            entry,
            live,
        )
        return True
    return False


def _peek_superseded_enabled(name: str) -> bool:
    """Whether the receipt says the superseded external app was ENABLED.

    READ-ONLY on purpose. The caller must apply the value, persist the installed
    record, and only THEN call :func:`_mark_superseded_enabled_applied`. Consuming
    the receipt here instead would reintroduce the very bug this restore exists to
    fix: if `_write_installed()` is interrupted after the flag was marked applied,
    the next boot finds no installed record AND a spent receipt, so the builtin
    registers at its `defaultEnabled: false` and the user's enabled app comes back
    off — permanently, because the receipt can only be spent once.

    Needed at all because a takeover interrupted after the archive rename but
    before the metadata write leaves the next boot with no installed record: the
    in-process hand-off (`superseded_enabled`) is gone.
    """
    receipt = _superseded_receipt_path(name)
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("enabledApplied"):
        return False
    return bool(data.get("enabled"))


def _mark_superseded_enabled_applied(name: str) -> None:
    """Spend the receipt's enabled flag, AFTER the installed record is persisted.

    Marked consumed rather than honoured on every boot: otherwise a user who later
    DISABLES the builtin would have it switched back on at the next start, which is
    the same class of surprise in the opposite direction.

    A failure here is non-fatal and only logged. The record was already written, so
    the next boot takes the `existing is not None` path and never consults the
    receipt — the flag is a backstop, and leaving it unspent cannot re-enable an app
    the user still has installed.
    """
    receipt = _superseded_receipt_path(name)
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        data["enabledApplied"] = True
        atomic_write(receipt, json.dumps(data, indent=2) + "\n")
    except (OSError, ValueError) as exc:
        logger.warning("Restored %r enabled but could not spend its receipt: %s", name, exc)
        return
    logger.warning(
        "Restored the enabled state for %r from its supersede receipt: the takeover "
        "was interrupted before the app metadata was written.",
        name,
    )


def _archive_superseded_install(name: str, builtin_version: str = "") -> bool:
    """Move a graduated external install aside so the builtin can register.

    Nothing is deleted: the whole directory is *renamed* to
    ``~/.kiro/crew/apps-superseded/<name>-<timestamp>``, which keeps the user's
    copy (and its secrets) recoverable, and sits outside ``apps/`` so it is not
    enumerated as an installed app.  ``data/`` is then moved back into the fresh
    app dir, because the builtin reads the same
    ``apps/<name>/data`` path -- that is what carries the user's pending edit
    requests across the graduation.

    Returns True only when the app dir was actually moved aside.
    """
    src = app_dir(name)
    # Read the user's enabled state BEFORE the rename takes installed.json out of
    # reach — it goes into the receipt so a recovery boot can restore it.
    _pre = _read_installed(name)
    was_enabled = bool(_pre.enabled) if _pre is not None else False
    # Never rename a symlinked app dir: the link target lives outside the apps
    # tree and moving it would relocate data we do not own.
    if src.is_symlink() or not src.is_dir():
        logger.warning("Not superseding %r: app dir is a symlink or missing", name)
        return False
    try:
        if not src.resolve().is_relative_to(apps_dir().resolve()):
            logger.warning("Not superseding %r: resolves outside the apps dir", name)
            return False
    except OSError as exc:
        logger.warning("Not superseding %r: %s", name, exc)
        return False

    archive_root = config_dir() / _SUPERSEDED_ARCHIVE_DIRNAME
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
        archive = archive_root / f"{name}-{stamp}"
        suffix = 1
        while archive.exists():
            archive = archive_root / f"{name}-{stamp}-{suffix}"
            suffix += 1
        os.rename(src, archive)
    except OSError as exc:
        logger.warning(
            "Not superseding %r: could not move the existing install aside: %s",
            name,
            exc,
        )
        return False

    logger.warning(
        "Superseding user-installed %r with the bundled builtin. The previous "
        "install was moved to %s (nothing was deleted); its data/ is carried "
        "over into %s.",
        name,
        archive,
        src,
    )
    # Record the takeover so it can never run twice: a later, deliberate
    # reinstall of the external app is the user's choice and must stick.
    _record_superseded(name, archive, builtin_version, enabled=was_enabled)
    # Carry data/ forward.  This must SUCCEED or be fully undone -- it cannot be
    # "non-fatal".  A read-only or cross-device archive makes the rename fail,
    # and the app's own backend creates ``data/queue`` at import (see
    # ``design_tweak/backend/server.py``), so the moment the builtin registers
    # there IS a live ``data/`` again.  ``_finish_interrupted_supersede`` refuses
    # to clobber a live ``data/``, so the recovery pass would then skip forever
    # and the archived pending queue would be invisible to the user for good.
    try:
        src.mkdir(parents=True, exist_ok=True)
        old_data = archive / "data"
        if old_data.is_dir() or old_data.is_symlink():
            os.rename(old_data, src / "data")
    except OSError as exc:
        logger.warning(
            "Superseding %r: could not MOVE data/ forward from %s (%s); copying instead.",
            name,
            archive,
            exc,
        )
        if not _copy_data_forward(name, archive, src):
            # Neither move nor copy worked. Leave the user exactly as they were
            # rather than registering a builtin whose data is stranded.
            _restore_superseded_install(name, archive, src)
            return False
    return True


def _copy_data_forward(name: str, archive: Path, src: Path) -> bool:
    """Copy ``<archive>/data`` to ``<src>/data`` when the rename could not move it.

    A copy leaves the archive's copy intact, so the user still has the original
    even in the degraded case. A PARTIAL copy is removed before returning False:
    a half-written ``data/`` is worse than none, because it both loses requests
    and looks like a live install to :func:`_finish_interrupted_supersede`.
    """
    old_data = archive / "data"
    if not (old_data.is_dir() or old_data.is_symlink()):
        return True  # nothing to carry
    dest = src / "data"
    try:
        shutil.copytree(old_data, dest, symlinks=True, dirs_exist_ok=True)
    except (OSError, shutil.Error) as exc:
        logger.warning("Could not copy data/ forward for %r from %s: %s", name, archive, exc)
        shutil.rmtree(dest, ignore_errors=True)
        return False
    logger.warning(
        "Carried data/ forward for %r by COPY; the original remains in %s.", name, archive
    )
    return True


def _restore_superseded_install(name: str, archive: Path, src: Path) -> None:
    """Undo a takeover whose data could neither be moved nor copied.

    Puts the user's install back where it was and removes the one-time receipt,
    so a later boot can retry rather than leaving the app permanently half
    migrated. Best-effort by necessity -- it runs because the filesystem is
    already refusing operations -- so every failure is logged loudly with the
    archive path, which is where the user's data still is.
    """
    try:
        shutil.rmtree(src, ignore_errors=True)  # the empty/partial dir we created
        os.rename(archive, src)
    except OSError as exc:
        logger.error(
            "Could not restore %r after a failed takeover: its install is still at "
            "%s and must be moved back by hand. Error: %s",
            name,
            archive,
            exc,
        )
        return
    try:
        _superseded_receipt_path(name).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Restored %r but could not remove its supersede receipt: %s", name, exc)
    logger.warning(
        "Rolled back the takeover of %r: data/ could not be carried forward, so the "
        "user's install was put back and the builtin was NOT registered.",
        name,
    )


def _app_declares_backend(app_data: dict[str, Any]) -> bool:
    """Whether a manifest declares a backend the gateway proxy can reach.

    Either shape counts: a gateway-spawned ``backend.entryPoint``, or a
    resolvable loopback ``mcpServers`` URL.  Both are proxied, and the proxy
    refuses a request outright when the app has no ``.app_secret``, so both must
    earn one.  An app with neither declares no backend and gets no secret.
    """
    if app_data.get("backend", {}).get("entryPoint"):
        return True
    return resolve_mcp_backend_url(app_data.get("mcpServers")) is not None


def register_builtin_apps() -> int:
    """Register built-in dashboard features as app entries.

    Called once at Gateway startup.  Idempotent — updates existing entries
    without removing user customizations.  Returns the number of apps
    registered or updated.

    Each app definition is validated before registration.  Invalid definitions
    are skipped with a warning log — they do not affect other apps.

    The ``defaultEnabled`` field (default: True) controls the initial enabled
    state for newly registered apps.  Existing apps preserve their user-set
    enabled state regardless of the definition's ``defaultEnabled`` value.

    Sources (merged, hardcoded list takes precedence on name collision):
    1. ``_BUILTIN_APPS`` hardcoded list — EMPTY since every builtin moved to a file
       manifest; kept as the ADD-only precedence seam for editions and tests
    2. Auto-discovered from ``builtins/`` directory via ``discovery.py``
    3. Edition-contributed builtins from the active PlatformContext's
       ``AppsLoader.manifest_sources()`` (empty in standalone; the internal
       companion contributes its feature apps).  ADD-only: the hardcoded list
       and the package's own builtins still take precedence on name collision.
    """
    # Merge hardcoded list with auto-discovered builtins + edition-contributed
    # builtins (PlatformContext).  Standalone contributes nothing extra
    # (manifest_sources == []), so ``discovered`` is exactly the package's
    # builtins/ dir — unchanged from today.
    discovered = discover_builtin_apps()
    discovered_names = {a["name"] for a in discovered}
    for app_data in _edition_builtin_apps():
        if app_data["name"] not in discovered_names:
            discovered_names.add(app_data["name"])
            discovered.append(app_data)
    hardcoded_names = {a["name"] for a in _BUILTIN_APPS}

    # Clean up apps that have been escalated to built-in surfaces, merged into
    # an existing surface, or removed from the fork — delete stale installed
    # state so they don't linger in the App Store / nav after the change.
    #   - knowledge: promoted from App Store to registerBuiltinSurface()
    #   - orchestrated: Autopilot merged into the unified Chat surface (mode flag)
    #   - board: removed from the fork (mirrors the upstream project, alongside
    #     the Channels hide); drop stale beta-install dirs so the
    #     orphaned entry doesn't resurface in the App Store Browse grid.
    _escalated = ["knowledge", "orchestrated", "board"]
    for esc_name in _escalated:
        esc_dir = app_dir(esc_name)
        # Never follow a symlinked app dir: iterdir()/rmtree would land on the
        # link target and delete data OUTSIDE the apps tree. Also require the
        # resolved path to stay contained under apps_dir().
        if esc_dir.is_symlink():
            logger.warning(
                "Skipping escalation cleanup for %r: app dir is a symlink", esc_name
            )
            continue
        if not esc_dir.is_dir():
            continue
        try:
            if not esc_dir.resolve().is_relative_to(apps_dir().resolve()):
                logger.warning(
                    "Skipping escalation cleanup for %r: resolves outside apps dir",
                    esc_name,
                )
                continue
        except OSError as exc:
            logger.warning("Skipping escalation cleanup for %r: %s", esc_name, exc)
            continue
        # Only remove a POSITIVELY identified legacy builtin install: an
        # unrelated local/registry/external app that merely shares the name
        # must never be deleted (it may hold user code and secrets).
        #
        # PIN-FIRST: the app directory descriptor is pinned
        # BEFORE any validation, and installed.json / data/ are inspected
        # RELATIVE to that pinned descriptor. A rename swapping the directory
        # between validation and deletion can therefore never redirect the
        # delete: verdict and deletion refer to the same inode by
        # construction.
        if not _dirfd_ops_supported() or not hasattr(os, "O_DIRECTORY"):
            # No POSIX dir_fd primitives (Windows): validation and deletion
            # cannot be pinned to the same inode, so a rename between them
            # could delete an unvalidated replacement directory. Fail
            # closed — leave legacy-builtin cleanup to the operator here.
            logger.info(
                "Skipping escalation cleanup for %r: platform lacks dir_fd "
                "primitives to pin validation to deletion — remove the "
                "directory manually if no longer needed", esc_name,
            )
            continue
        parent_fd = -1
        fd = -1
        try:
            # Anchor at the trusted apps root, then open the app dir RELATIVE
            # to that descriptor with O_NOFOLLOW: containment holds by
            # construction and cannot be raced by renames/symlinks.
            parent_fd = os.open(str(apps_dir()), os.O_RDONLY | os.O_DIRECTORY)
            fd = os.open(
                esc_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_DIRECTORY,
                dir_fd=parent_fd,
            )
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise OSError("not a directory")

            # installed.json read through the pinned descriptor: O_NOFOLLOW
            # + fstat-regular on the OPENED fd — a symlinked or mid-race
            # swapped meta file is refused by the kernel atomically.
            meta = None
            meta_fd = -1
            try:
                meta_fd = os.open(
                    INSTALLED_META_FILENAME,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
                mst = os.fstat(meta_fd)
                if not stat.S_ISREG(mst.st_mode):
                    raise OSError("installed.json is not a regular file")
                with os.fdopen(meta_fd, "r", encoding="utf-8") as fh:
                    meta_fd = -1  # ownership transferred to fdopen
                    meta = json.load(fh)
            except (OSError, ValueError):
                meta = None
            finally:
                if meta_fd >= 0:
                    os.close(meta_fd)
            if not isinstance(meta, dict) or meta.get("origin") != "builtin":
                logger.info(
                    "Keeping app dir %r during escalation cleanup: origin=%r "
                    "is not a legacy builtin",
                    esc_name,
                    meta.get("origin") if isinstance(meta, dict) else None,
                )
                continue

            # Preserve user data/ across the escalation — inspected through
            # the same pinned descriptor. A symlinked data/ or any error we
            # cannot classify fails closed (keep).
            try:
                data_fd = os.open(
                    "data",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_DIRECTORY,
                    dir_fd=fd,
                )
                try:
                    has_data = bool(os.listdir(data_fd))
                finally:
                    os.close(data_fd)
            except (FileNotFoundError, NotADirectoryError):
                has_data = False
            except OSError as exc:
                # Symlinked data/ (ELOOP) or unreadable — fail closed: keep.
                logger.warning(
                    "Skipping escalation cleanup for %r: cannot inspect data/: %s",
                    esc_name, exc,
                )
                continue
            if has_data:
                # No partial deletion: keep everything and leave removal to
                # the operator.
                logger.info(
                    "Keeping escalated builtin %r: data/ is non-empty — remove "
                    "the directory manually if no longer needed", esc_name,
                )
                continue

            _rmtree_dirfd(fd)
            os.close(fd)
            fd = -1
            # Unlink the NAME only if the entry still refers to the pinned
            # inode: a directory swapped in after the pin is left untouched
            # (rmdir would also refuse a non-empty swap, but check anyway).
            try:
                st2 = os.stat(esc_name, dir_fd=parent_fd, follow_symlinks=False)
                if (st2.st_ino, st2.st_dev) == (st.st_ino, st.st_dev):
                    os.rmdir(esc_name, dir_fd=parent_fd)
                    logger.info(
                        "Removed escalated app %r (now a built-in surface)",
                        esc_name,
                    )
                else:
                    logger.warning(
                        "Escalation cleanup for %r: directory entry changed "
                        "after pin — leaving the new entry in place", esc_name,
                    )
            except FileNotFoundError:
                pass
        except OSError as exc:
            logger.warning("Escalation cleanup failed for %r (kept): %s", esc_name, exc)
        finally:
            if fd >= 0:
                os.close(fd)
            if parent_fd >= 0:
                os.close(parent_fd)
    # Discovered apps that aren't already in the hardcoded list
    extra = [a for a in discovered if a["name"] not in hardcoded_names]
    all_builtins = list(_BUILTIN_APPS) + extra

    count = 0
    for app_data in all_builtins:
        # Validate definition — skip invalid entries without affecting others
        errors = _validate_builtin_app(app_data)
        if errors:
            logger.warning(
                "Skipping invalid builtin app definition %r: %s",
                app_data.get("name", "<unnamed>"),
                "; ".join(errors),
            )
            continue

        name = app_data["name"]

        # Lenient migratedTo handling: warn but don't block registration
        migrated_to_raw = app_data.get("migratedTo", "")
        migrated_to_effective = _effective_migrated_to(app_data)
        if migrated_to_raw and not migrated_to_effective:
            logger.warning(
                "Builtin app %r has invalid migratedTo format %r — field ignored",
                name,
                migrated_to_raw,
            )
        elif migrated_to_effective:
            target_name = migrated_to_effective.split(":", 1)[1]
            if target_name != name:
                logger.warning(
                    "Builtin app %r migratedTo target %r differs from app name "
                    "— this may break data directory sharing",
                    name,
                    migrated_to_effective,
                )

        existing = _read_installed(name)

        dest = app_dir(name)
        dest.mkdir(parents=True, exist_ok=True)

        # A pre-existing entry this function did not write belongs to the USER:
        # they installed an app that happens to share this builtin's name. Taking
        # it over is unrecoverable -- see _builtin_owns_install() -- so stand down
        # entirely and leave their install exactly as it is.
        #
        # The ONE exception is an app this package graduates: the external app is
        # positively identified by its manifest repository, moved aside intact
        # (never deleted) and its data/ carried forward, so the graduated user
        # receives the builtin instead of silently keeping the retired external
        # copy forever. It is a ONE-TIME migration and never a downgrade -- see
        # _superseding_external_install().
        superseded_enabled = False
        # Before deciding anything, heal a takeover that died between its two
        # renames — the archive is also the "already ran" marker, so the
        # migration would otherwise never come back to finish itself and the
        # user's pending requests would stay stranded. No-op in every other
        # state, including a deliberate reinstall (which has a live data/).
        _finish_interrupted_supersede(name)
        if existing is None:
            # No installed record: either a first install, or a takeover that died
            # before the metadata write. In the latter case the receipt still knows
            # the user had this app ENABLED, so honour it once.
            superseded_enabled = _peek_superseded_enabled(name)
        if existing and not _builtin_owns_install(existing):
            builtin_version = str(app_data["version"])
            if _superseding_external_install(name, builtin_version) and (
                _archive_superseded_install(name, builtin_version)
            ):
                superseded_enabled = existing.enabled
                existing = None
            else:
                logger.warning(
                    "Not registering builtin %r: a user-installed app already occupies "
                    "%s (source=%r, origin=%r). Leaving its manifest and metadata "
                    "untouched; the builtin is not registered on this host.",
                    name, app_dir(name), existing.source, existing.origin,
                )
                continue

        if existing:
            # Only update version + displayName, preserve user state
            existing.version = app_data["version"]
            existing.displayName = app_data["displayName"]
            existing.updatedAt = _now_iso()
            has_ui_bundle = bool(app_data.get("ui", {}).get("entry"))
            existing.origin = "local" if has_ui_bundle else "builtin"
            existing.resources = "gateway"
            existing.lifecycle = "locked"
            # Sync migratedTo from definition (overwrite stale values)
            existing.migratedTo = _effective_migrated_to(app_data)
            _write_installed(name, existing)
        else:
            # Use defaultEnabled from definition (defaults to True for backward compat)
            default_enabled = app_data.get("defaultEnabled", True)
            # A superseded external install keeps the user's enabled state: they
            # had the app switched on, and graduating it must not silently turn
            # it off. It still passes the governance gate below.
            if superseded_enabled:
                default_enabled = True
            # Governance chokepoint. enable_app() normally enforces the ``apps``
            # activation allowlist, but a *default-enabled* builtin is persisted
            # here on first registration and never routes through enable_app() —
            # which would let it bypass a host deny-by-default policy. Re-apply the
            # same gate so a governance-denied app registers DISABLED. This is a
            # no-op for default-disabled builtins (the historical case).
            if default_enabled and _app_activation_denied(name):
                default_enabled = False
            meta = InstalledApp(
                name=name,
                version=app_data["version"],
                displayName=app_data["displayName"],
                enabled=default_enabled,
                installedAt=_now_iso(),
                source="builtin",
                origin="builtin",
                resources="gateway",
                lifecycle="locked",
                migratedTo=_effective_migrated_to(app_data),
            )
            _write_installed(name, meta)
            # Spend the receipt ONLY now that the record is durable. Marking it
            # before this write is what let an interrupted write lose the user's
            # enabled state for good — the flag can be spent once, so the next
            # boot would have found no record AND no flag, and defaulted to off.
            if superseded_enabled:
                _mark_superseded_enabled_applied(name)

        # Persist manifest so dashboard can show full info
        atomic_write(
            dest / APP_MANIFEST_FILENAME,
            json.dumps(app_data, indent=2) + "\n",
        )

        # Built-in apps with a backend need an app secret so the gateway
        # proxy can authenticate requests to them.  Generate once; preserve
        # existing secret across restarts to keep live backends valid.  A
        # backend is either a gateway-spawned entryPoint OR a resolvable
        # loopback mcpServers URL (self-managed apps) — both go through the
        # proxy, which 502s without a secret, so both must get one.
        if _app_declares_backend(app_data):
            secret_path = dest / ".app_secret"
            if not secret_path.is_file():
                # circular import: token_auth → app_secret_store → manager
                # token_auth imports app_secret_store, which transitively
                # imports the manager module's app-directory helpers.
                # Importing at module scope here would create a cycle, so
                # we defer to the function body.
                from kiro_crew.dashboard.token_auth import generate_app_secret, write_app_secret

                write_app_secret(name, generate_app_secret())
            # Invalidate the proxy secret cache so the newly-written (or
            # previously existing) secret is picked up on the next request.
            try:
                # circular import: routes → manager
                # kiro_crew.apps.routes imports from kiro_crew.apps.manager
                # at module load, so we cannot import routes at the top of
                # this file without creating a cycle.
                from kiro_crew.apps.routes import invalidate_app_secret_cache

                invalidate_app_secret_cache(name)
            except Exception:
                pass  # routes module may not be importable during bootstrap

        count += 1

    if count:
        logger.info("Registered %d built-in app(s)", count)

    # Warm the orphan cache after registration
    detect_orphaned_builtins(force_refresh=True)

    return count


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------

_orphaned_builtins_cache: set[str] | None = None


def detect_orphaned_builtins(*, force_refresh: bool = False) -> set[str]:
    """Return set of orphaned builtin app names.

    Scans apps_dir for builtin apps not in _BUILTIN_APPS list or
    auto-discovered from the builtins/ directory.
    Result is cached after first call; pass force_refresh=True to re-scan
    (called on mc:apps-changed events).
    """
    global _orphaned_builtins_cache
    if _orphaned_builtins_cache is not None and not force_refresh:
        return _orphaned_builtins_cache

    # Combine hardcoded list + auto-discovered names + edition-contributed
    # builtins (PlatformContext).  Standalone adds nothing (manifest_sources ==
    # [] and bundled_app_names() == OSS builtins already covered); the internal
    # companion's feature apps are recognized as builtins here so they are not
    # mis-flagged as orphans after registration.  ``bundled_app_names()`` is
    # also honored as a declaration so a declared app whose manifest dir is
    # momentarily missing is not mis-orphaned.
    builtin_names = {app["name"] for app in _BUILTIN_APPS}
    builtin_names.update(app["name"] for app in discover_builtin_apps())
    builtin_names.update(app["name"] for app in _edition_builtin_apps())
    builtin_names.update(_edition_bundled_app_names())

    orphaned: set[str] = set()
    root = apps_dir()
    if not root.is_dir():
        _orphaned_builtins_cache = orphaned
        return orphaned
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        meta = _read_installed(entry.name)
        if meta and meta.origin == "builtin" and entry.name not in builtin_names:
            orphaned.add(entry.name)
    _orphaned_builtins_cache = orphaned
    return orphaned


def invalidate_orphan_cache() -> None:
    """Called when apps change (install/uninstall/cleanup)."""
    global _orphaned_builtins_cache
    _orphaned_builtins_cache = None


# ---------------------------------------------------------------------------
# Migration cleanup
# ---------------------------------------------------------------------------


def cleanup_migrated_builtin(name: str) -> AppResult:
    """Remove orphaned builtin metadata after its functionality was folded into core.

    Matches by app NAME (not migratedTo metadata) — existing installs from before
    the migration mechanism won't have migratedTo set. The presence of `name` in
    _MIGRATED_BUILTINS is the authoritative signal.

    Preserves data/ directory. Removes installed.json and app.json only.
    Idempotent: returns ok=True if already cleaned up.
    """
    from kiro_crew.apps.builtins import _MIGRATED_BUILTINS

    if name not in _MIGRATED_BUILTINS:
        return AppResult(ok=False, name=name, error="not a migrated builtin")

    if not _check_path_safety(name):
        return AppResult(ok=False, name=name, error=f"unsafe app name: {name!r}")

    meta = _read_installed(name)
    if not meta:
        # Already cleaned up or was never installed — success (idempotent).
        logger.debug("cleanup_migrated_builtin: %s not installed (already clean)", name)
        return AppResult(ok=True, name=name, message="not installed — nothing to clean up")

    # If the install has origin != builtin, a standalone replacement already took
    # over — nothing to clean up.
    if meta.origin != "builtin":
        return AppResult(
            ok=True,
            name=name,
            message="already migrated — standalone version is in place",
        )

    # Perform cleanup — remove metadata files, preserve data/
    dest = app_dir(name)
    installed_path = dest / INSTALLED_META_FILENAME
    manifest_path = dest / APP_MANIFEST_FILENAME

    try:
        if manifest_path.is_file():
            manifest_path.unlink()
        if installed_path.is_file():
            installed_path.unlink()
    except OSError as exc:
        logger.error("cleanup_migrated_builtin: failed to clean up %s: %s", name, exc)
        return AppResult(
            ok=False,
            name=name,
            error=f"failed to clean up app metadata: {exc}",
            error_code="io_error",
        )

    # Invalidate orphan cache since we removed an orphaned entry
    invalidate_orphan_cache()

    logger.info("Cleaned up migrated builtin %s (data preserved)", name)
    return AppResult(
        ok=True,
        name=name,
        message="cleaned up migrated builtin entry, data preserved",
    )
