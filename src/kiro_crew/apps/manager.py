"""App Manager — install, uninstall, enable, disable lifecycle for KiroCrew apps.

Apps are installed to ``~/.kirocrew/apps/{name}/``.  Each installed app has an
``installed.json`` metadata file tracking version, timestamp, and enabled state.

The manager validates manifests, copies app files, and delegates resource
registration (agents, skills, crons) to bridge functions.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest
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
    """Return the root directory for installed apps: ``~/.kirocrew/apps/``."""
    return config_dir() / "apps"


def app_dir(name: str) -> Path:
    """Return the directory for a specific installed app."""
    return apps_dir() / name


def app_data_dir(name: str) -> Path:
    """Return the app-scoped data directory: ``~/.kirocrew/apps/{name}/data/``."""
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
    # Check minKiroCrewVersion
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


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install_app(source: str | Path) -> AppResult:
    """Install an app from a local directory path.

    1. Validate manifest
    2. Copy to ``~/.kirocrew/apps/{name}/``
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

    # Validate
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

    # Copy app files to install directory
    # Preserve existing data/ directory (left behind by prior uninstall --keep-data)
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
            shutil.rmtree(dest)
        shutil.copytree(source, dest, dirs_exist_ok=True)

        # Restore preserved data/ (overwrite empty data/ from source package)
        if tmp_data.is_dir():
            restored = dest / "data"
            if restored.exists():
                shutil.rmtree(restored)
            shutil.move(str(tmp_data), str(restored))
    except OSError as exc:
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


def update_app(source: str | Path) -> AppResult:
    """Update an already-installed app from a local directory path.

    1. Validate new manifest
    2. Preserve ``data/`` directory
    3. Replace app files
    4. Update ``installed.json``
    """
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        return AppResult(ok=False, error=f"source is not a directory: {source}")

    errors = _validate_source_path(source)
    if errors:
        return AppResult(ok=False, error="; ".join(errors))

    manifest = AppManifest.from_json_file(source / APP_MANIFEST_FILENAME)
    name = manifest.name
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
        shutil.copytree(source, dest, dirs_exist_ok=True)

        # Restore data
        if tmp_data.is_dir():
            restored = dest / "data"
            if restored.exists():
                shutil.rmtree(restored)
            shutil.move(str(tmp_data), str(restored))
        # Restore secret
        if tmp_secret.is_file():
            shutil.move(str(tmp_secret), str(dest / ".app_secret"))
    except OSError as exc:
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

    # Update metadata — preserve enabled state and install time
    meta = InstalledApp(
        name=name,
        version=manifest.version,
        displayName=manifest.displayName,
        enabled=existing.enabled,
        installedAt=existing.installedAt,
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


def uninstall_app(name: str, *, keep_data: bool = False) -> AppResult:
    """Uninstall an app by removing its directory.

    If *keep_data* is True, the ``data/`` subdirectory is preserved.
    Resource deregistration should be done before calling this.
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
    slack-bound profiles — CR-284272012.)  Best-effort beyond the always-on
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
        # hard fail (CR-284272012).
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
# React tree directly (no ESM bundle) and their routes are hardcoded in
# App.tsx.  The registration here is metadata-only so the App Store can
# display them alongside installable apps.
#
# Default-disabled policy: every builtin app ships with ``defaultEnabled:
# False`` so a fresh install presents a minimal sidebar (core surfaces only)
# instead of every app at once. Apps are opt-in from the App Store Browse
# tab. Because ``register_builtin_apps()`` applies ``defaultEnabled`` only on
# first registration and preserves user state on restart, existing users keep
# whatever they already enabled — this only changes the out-of-the-box
# experience for new installs.

_BUILTIN_APPS: list[dict[str, Any]] = [
    {
        "name": "agent-worlds",
        "version": "1.0.0",
        "displayName": "Agent Worlds",
        "description": "Visualize your agents in interactive pixel-art scenes",
        "author": "kirocrew",
        "tags": ["visualization", "agents"],
        "defaultEnabled": False,
        "iconUrl": "/app-assets/worlds/icon.svg",
        "heroImage": "/app-assets/worlds/hero-light.svg",
        "heroImageDark": "/app-assets/worlds/hero-dark.svg",
        "ui": {
            "pages": [{"route": "/worlds", "label": "Worlds", "icon": "Gamepad2"}],
        },
    },
    {
        "name": "channels",
        "version": "1.0.0",
        "displayName": "Channels",
        "description": "Multi-agent collaboration channels with persistent context",
        "author": "kirocrew",
        "tags": ["collaboration", "agents"],
        "defaultEnabled": False,
        # Hidden from the App Store Browse grid (opt-in via `kirocrew app enable channels`).
        # Code and routes remain fully intact; this only gates store visibility.
        "hidden": True,
        "permissions": {
            "api": ["/api/channels"],
            "events": ["channel", "channel_message"],
        },
        "ui": {
            "pages": [{"route": "/channels", "label": "Channels", "icon": "Users"}],
        },
    },
    {
        "name": "projects",
        "version": "1.0.0",
        "displayName": "Task Runner",
        "description": "Autonomous multi-step task execution — compose ideas, generate plans, and run them to completion",
        "author": "kirocrew",
        "tags": ["tasks", "autonomy", "execution"],
        "defaultEnabled": False,
        "iconUrl": "/app-assets/projects/icon.svg",
        "heroImage": "/app-assets/projects/hero-light.svg",
        "heroImageDark": "/app-assets/projects/hero-dark.svg",
        "ui": {
            "pages": [{"route": "/projects", "label": "Task Runner", "icon": "ClipboardCheck"}],
        },
    },
    # -------------------------------------------------------------------------
    # Example: opt-in builtin app (defaultEnabled: false)
    #
    # Uncomment to test the defaultEnabled feature. This app will appear in the
    # Browse tab for discovery and can be enabled by the user.
    #
    # {
    #     "name": "example-opt-in",
    #     "version": "1.0.0",
    #     "displayName": "Example Opt-In Feature",
    #     "description": "A demonstration of a builtin app that defaults to disabled",
    #     "author": "kirocrew",
    #     "tags": ["example"],
    #     "defaultEnabled": False,
    #     "ui": {
    #         "pages": [
    #             {"route": "/example-opt-in", "label": "Example", "icon": "FlaskConical"}
    #         ],
    #     },
    # },
]


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
    extra apps, byte-for-byte today's behavior.  The Amazon companion returns a
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
    discovery, so this is a no-op for standalone.  The Amazon companion declares
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
    1. ``_BUILTIN_APPS`` hardcoded list (legacy, being phased out)
    2. Auto-discovered from ``builtins/`` directory via ``discovery.py``
    3. Edition-contributed builtins from the active PlatformContext's
       ``AppsLoader.manifest_sources()`` (empty in standalone; the Amazon
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
    #   - board: removed from the fork (mirrors upstream CR-289326017, alongside
    #     the Channels hide P472750613); drop stale beta-install dirs so the
    #     orphaned entry doesn't resurface in the App Store Browse grid.
    _escalated = ["knowledge", "orchestrated", "board"]
    for esc_name in _escalated:
        esc_dir = app_dir(esc_name)
        if esc_dir.is_dir():
            shutil.rmtree(esc_dir, ignore_errors=True)
            logger.info("Removed escalated app %r (now a built-in surface)", esc_name)
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
        # existing secret across restarts to keep live backends valid.
        if app_data.get("backend", {}).get("entryPoint"):
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
    # [] and bundled_app_names() == OSS builtins already covered); the Amazon
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
