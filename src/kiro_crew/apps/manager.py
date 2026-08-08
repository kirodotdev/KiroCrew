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
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    config_dir,
    config_local_path,
    config_path,
    write_config_atomically,
)
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
    from kiro_crew.apps.version import check_min_version

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

    # Withdraw the execution grant FIRST, and abort the whole uninstall if it
    # cannot be withdrawn.
    #
    # A grant is keyed on the app NAME and nothing else, so one left behind admits
    # a DIFFERENT app later installed under this name — in-process code execution
    # with no consent prompt, because the gate just sees a name it was told to
    # trust. Doing this AFTER the files were deleted (as this did) produced a state
    # the user could not recover from: the app is gone, so there is nothing left to
    # uninstall and no retry that would clear the grant, while the name stays
    # armed. Ordering it first makes the failure retryable — nothing has been
    # destroyed, the user fixes the cause (typically an overlay-owned setting) and
    # runs uninstall again. Same reasoning as the revoke path, which runs teardown
    # before its config write for exactly this reason.
    try:
        # Recorded BEFORE the withdrawal so a failed delete below can put back
        # exactly what was there — and only when there WAS something. Restoring a
        # grant the app never held would be granting, not restoring.
        had_grant = _has_trust_grant(name)
        _drop_trust_grant(name)
    except Exception as exc:  # noqa: BLE001 - refuse rather than half-uninstall
        logger.warning("trust-grant cleanup on uninstall of %r failed", name, exc_info=True)
        return AppResult(
            ok=False,
            name=name,
            error=(
                f"not uninstalling {name!r}: its third-party execution grant could "
                f"not be removed ({exc}). The grant is keyed on the name, so removing "
                f"the app while it stands would let any future app installed under "
                f"this name run code without asking. Clear the cause and retry."
            ),
            error_code="trust_grant_not_removed",
        )

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
        # The delete failed, so the app is STILL INSTALLED — but its grant was
        # withdrawn above, which would leave a trusted app silently stripped of the
        # permission the operator gave it, from an operation that did not even
        # succeed. Put it back.
        #
        # Restoring is not widening: this re-adds the grant the operator had already
        # made, to an app that is still on disk, returning the exact state that
        # existed before this call. The alternative shapes are both worse. Deferring
        # the withdrawal until after a successful delete re-opens the hole the
        # pre-delete ordering exists to close — a withdrawal that then fails leaves
        # the app GONE with its name still armed, and no app left to uninstall means
        # no retry can ever clear it. Leaving the grant withdrawn here is fail-safe
        # but silently punitive. Restoring keeps the withdrawal-first ordering (so a
        # withdrawal failure stays retryable with nothing destroyed) AND leaves a
        # failed uninstall with no side effect on trust.
        restore_note = ""
        try:
            _restore_trust_grant(name, had_grant)
        except Exception as restore_exc:  # noqa: BLE001 - report, never mask the real error
            logger.warning(
                "could not restore %r's execution grant after a failed uninstall",
                name,
                exc_info=True,
            )
            restore_note = (
                f" Its third-party execution grant was withdrawn and could not be "
                f"restored ({restore_exc}); re-grant it in Settings if you still "
                f"want this app to run its own code."
            )
        return AppResult(
            ok=False, name=name, error=f"failed to remove app: {exc}{restore_note}"
        )

    logger.info("Uninstalled app %s (keep_data=%s)", name, keep_data)

    # Withdraw the grant a SECOND time, now that the files are actually gone.
    #
    # The first withdrawal above deliberately runs BEFORE the delete so that a
    # failure is retryable with nothing destroyed. That ordering, though, leaves a
    # cross-process window a dashboard grant can land in — no in-process lock helps,
    # because this runs under `kirocrew app uninstall` in a DIFFERENT process:
    #
    #   this process: drop grant (no-op, none yet) ................ then ... rmtree
    #   dashboard:      app exists? yes -> write grant -> app still exists? yes -> 200
    #
    # Every check on both sides passes, and the grant is left standing over a name
    # no app occupies — the exact orphan both sides exist to prevent, and one that
    # would let a DIFFERENT app later installed under this name execute with no
    # consent prompt.
    #
    # Closing it needs no cross-process lock, only this ordering argument. The grant
    # is orphaned only if the write happened, the delete happened, AND the handler's
    # post-write existence check still saw the app. That check seeing the app means
    # it ran before this `rmtree` finished — so this second withdrawal, which runs
    # after the delete, necessarily runs after that write and therefore SEES the
    # grant. The handler's post-write check covers the opposite interleaving (delete
    # completes first, so the check finds nothing and rolls its own write back).
    # Between them the two guards leave no window, without either side blocking on
    # the other.
    residual = ""
    try:
        _drop_trust_grant(name)
    except Exception as exc:  # noqa: BLE001 - the app is already gone; report, never hide
        # Refusing the uninstall is not available here and would be a lie: the
        # files are deleted. So report it. A live grant over a name with no app is
        # precisely the state that must not stay quiet — it is invisible in the app
        # list (there is no app to show) and only surfaces when something new takes
        # the name.
        logger.warning(
            "app %r was uninstalled but its execution grant could not be withdrawn "
            "afterwards; the grant is still standing",
            name,
            exc_info=True,
        )
        residual = (
            f" WARNING: a third-party execution grant for {name!r} is still in "
            f"agent.apps_trusted and could not be removed ({exc}). Remove it in "
            f"Settings -> Security before installing anything under this name."
        )

    # Drop any dev-mode sentinel entry so an app later reinstalled under this
    # name does not inherit stale dev-mode serving/watching. Lazy import avoids
    # a module-level cycle (dev_mode imports from manager).
    try:
        from kiro_crew.apps.dev_mode import remove_dev_app

        remove_dev_app(name)
    except Exception:
        logger.debug("dev-mode cleanup on uninstall of %r failed", name, exc_info=True)
    return AppResult(ok=True, name=name, message=f"uninstalled {name}{residual}")


def trust_grant_removal_blocked(name: str) -> str | None:
    """Return why *name*'s execution grant could not be dropped, or ``None``.

    A read-only PRECONDITION. Both uninstall entry points run destructive,
    non-idempotent work (cron deregistration, the app's own ``onUninstall``
    script, backend stop, dependency cleanup) before they reach
    :func:`uninstall_app`, so a refusal discovered inside ``uninstall_app`` is
    not the retryable "nothing has been destroyed" case it was written as: it
    strands a half-removed app and re-runs ``onUninstall`` on every retry. Callers
    therefore ask this FIRST and abort while it is still free to abort — the same
    reason the cron cleanup is ordered ahead of the script.
    """
    # An overlay-owned grant cannot be dropped by writing config.json: the loader
    # deep-merges config.local.json OVER it and save() strips overlay-owned values
    # from the output, so the write is ineffective in both directions.
    #
    # Scoped to a grant this app actually holds. An overlay that pins
    # `apps_trusted` for OTHER apps says nothing about THIS uninstall, and gating
    # on the key's mere presence made every app un-uninstallable for any operator
    # who set it at all — a blanket refusal, not a grant-specific one.
    local = config_local_path()
    if local.is_file():
        try:
            raw_local = json.loads(local.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raw_local = {}  # the loader ignores an unreadable overlay, so do we
        agent_local = raw_local.get("agent") if isinstance(raw_local, dict) else None
        if isinstance(agent_local, dict):
            overlay_grants = agent_local.get("apps_trusted")
            # A non-list overlay value cannot express a grant for this app, so
            # there is nothing here that a write would have to survive.
            if isinstance(overlay_grants, list) and name in overlay_grants:
                return f"apps_trusted is set in {local}, which overrides config.json"

    path = config_path()
    if path.is_file():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            # Report rather than stay silent: a quiet bail here is precisely the
            # "uninstalled but still trusted" state the caller must not reach. The
            # write is still refused (it would erase everything else the file
            # holds) — it just is not refused quietly.
            return f"{path} is unreadable: {exc}"
    return None


def _drop_trust_grant(name: str) -> None:
    """Remove *name* from ``agent.apps_trusted``, if present.

    A no-op when the app held no grant, which is the common case. Refuses to write
    over an unparseable ``config.json`` for the same reason the trusted-apps
    endpoints do: ``KiroCrewConfig.load()`` degrades a corrupt file to defaults, so
    a blind load/save would erase everything else the file holds.
    """
    blocked = trust_grant_removal_blocked(name)
    if blocked:
        raise RuntimeError(blocked)

    # Operate on the BASE file's own list, not the merged view.
    #
    # `KiroCrewConfig.load()` deep-merges `config.local.json` OVER `config.json`,
    # and a list MERGE REPLACES rather than unions — so with base
    # `apps_trusted: ["foo"]` and overlay `["bar"]`, the merged value is `["bar"]`
    # and a merged-view check concludes `foo` holds no grant and removes nothing.
    # The base entry then survives the uninstall: inert while the overlay stands,
    # but live again the moment the operator edits or drops that overlay key, at
    # which point a DIFFERENT app installed under the name `foo` inherits a grant
    # nobody made for it. Reading merged state to decide a base-file write is the
    # bug; the two layers have to be reasoned about separately.
    #
    # Writing through `cfg.save()` cannot fix it either: save() deliberately
    # strips overlay-owned keys from its output, so the one key we need to rewrite
    # is exactly the one it will not emit. Hence a targeted edit of the raw base
    # document, which also keeps the blast radius to a single key instead of
    # re-serialising the whole config from the model.
    path = config_path()
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # RAISE rather than return: a silent bail here is precisely the
        # "uninstalled but still trusted" state the caller must not reach. The
        # write is still refused — it would erase everything else the file holds.
        raise RuntimeError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        return
    agent_raw = raw.get("agent")
    if not isinstance(agent_raw, dict):
        return
    base_grants = agent_raw.get("apps_trusted")
    # A non-list value cannot express a grant for this app, so there is nothing
    # here that would later admit a replacement under this name.
    if not isinstance(base_grants, list) or name not in base_grants:
        return
    agent_raw["apps_trusted"] = [a for a in base_grants if a != name]
    # Concurrency: this is the repo's standard config read-modify-write, and it
    # inherits that model exactly — no cross-process lock, atomic (tmp+rename) on
    # the way out so no reader can see a torn file. `read_config_for_update`'s own
    # docstring describes the same shape and the same residual exposure, and the
    # base branch has two dozen writers in it, `kirocrew config set` among them, so
    # a CLI write racing a dashboard write can drop the loser's settings today
    # regardless of this function. Closing that properly means locking at the config
    # layer for every writer at once, which is its own change; doing it for this one
    # writer would serialize it against nothing.
    #
    # What is in scope here is not adding exposure: the early returns above mean the
    # ordinary uninstall (no grant on the name) reaches no write at all, and a write
    # happens only when there really is a grant to withdraw — locked by
    # `test_uninstall_writes_no_config_at_all_when_there_is_no_grant`. The write is
    # also a single-key edit of the raw document rather than a re-serialisation of
    # the whole config, so what it can clobber is bounded to a concurrent edit that
    # lands inside the same read-to-write window.
    write_config_atomically(path, raw)
    logger.info("Dropped third-party trust grant for uninstalled app %s", name)
    # Audited, because this REVOKES an execution permission. The dashboard's revoke
    # endpoint emits its own SEL event, but this path runs from `kirocrew app
    # uninstall` — so a grant could be withdrawn with nothing in the security event
    # log to show it, and the log is what an operator reconstructs a trust timeline
    # from. A permission boundary that moves silently is exactly what SEL exists to
    # make visible; the log records the transition, not merely the request that
    # caused it. Emitted AFTER the write so it attests something that actually
    # happened, and never allowed to fail the uninstall: losing the audit line is
    # bad, refusing to complete a withdrawal because the audit sink is unavailable
    # is worse.
    try:
        sel().log_api_access(
            caller="cli",
            operation="app_trust_revoke",
            outcome="allowed",
            resources=f"{name}=grant_removed_on_uninstall",
        )
    except Exception:  # noqa: BLE001 - the withdrawal already happened
        logger.warning("could not audit the trust withdrawal for %r", name, exc_info=True)


def _has_trust_grant(name: str) -> bool:
    """Whether the BASE ``config.json`` currently grants *name* execution.

    Reads the base document, not the merged view, for the same reason
    :func:`_drop_trust_grant` writes to it: an overlay list REPLACES rather than
    unions, so the merged value answers a different question than "is there a base
    entry here to put back".
    """
    path = config_path()
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    agent_raw = raw.get("agent")
    if not isinstance(agent_raw, dict):
        return False
    grants = agent_raw.get("apps_trusted")
    return isinstance(grants, list) and name in grants


def _restore_trust_grant(name: str, had_grant: bool) -> None:
    """Put *name*'s grant back after an uninstall failed with the app still installed.

    A no-op when the app held no grant to begin with — restoring one it never had
    would be GRANTING execution permission as a side effect of a failed uninstall,
    which is the one thing this must never do. Also a no-op if a grant is already
    present, so a concurrent re-grant is not duplicated.
    """
    if not had_grant or _has_trust_grant(name):
        return
    path = config_path()
    raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"{path} does not hold a JSON object")
    agent_raw = raw.setdefault("agent", {})
    if not isinstance(agent_raw, dict):
        raise RuntimeError(f"{path} has a non-object agent section")
    grants = agent_raw.get("apps_trusted")
    agent_raw["apps_trusted"] = [*(grants if isinstance(grants, list) else []), name]
    write_config_atomically(path, raw)
    logger.info("Restored %s's trust grant after a failed uninstall", name)
    try:
        sel().log_api_access(
            caller="cli",
            operation="app_trust_restore",
            outcome="allowed",
            resources=f"{name}=grant_restored_after_failed_uninstall",
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not audit the trust restore for %r", name, exc_info=True)


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
        if existing and not _builtin_owns_install(existing):
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
