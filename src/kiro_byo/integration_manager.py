"""KiroCrew integration management used by the Kiro BYO KiroCrew App.

KiroCrew still has no public ordinary-App contribution point for an arbitrary
third-party ACP backend id. Kiro BYO therefore keeps a fail-closed compatibility
layer. The adapter uses a dedicated semantic HarnessAdapter transform. Compatible
KiroCrew updates do not require a new Kiro BYO package merely because their
version changed; real source-contract changes are still refused.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import crew12_patch, crew_dashboard_patch, crew11_model_labels
from .crew_native_probe import inspect_native_registration
from .crew_import_closure import byo_import_closure_issues
from .crew_retirement import plan_retired_generated_seams as _plan_retired_generated_seams

from .crew_contract_checks import (
    _PREFIX, _normalized_text, _top_level_bound_names, _literal_strings_bound_to,
    _export_contract_issues, compatibility_contract_issues, rc6_contract_issues,
    _crew_seam_is_symlink, _version_core, _install_plan_precommit_issue,
)


class IntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class IntegrationTarget:
    crew_version: str
    crew_package: Path
    site_packages: Path
    adapter_dir: Path
    baseline: str
    manifest: dict[str, Any]
    compatibility: dict[str, Any]

    @property
    def overlay_root(self) -> Path:
        return self.adapter_dir / "overlay"

    @property
    def generated_files(self) -> frozenset[str]:
        return _compat_file_paths(self.compatibility, "generated_files")

    @property
    def created_files(self) -> frozenset[str]:
        return _compat_file_paths(self.compatibility, "created_files")


# Crew 0.12 family, including RC builds; require actual source/permission validation.
_CREW_BASELINE = (0, 12, 0)


def _crew12_version_candidate(crew_version: str) -> bool:
    """0.12+ RC/stable syntax candidate; installed-source checks remain mandatory.

    Both SemVer (``0.12.0-rc.10``) and PEP 440 (``0.12.0rc10``) labels
    occur in KiroCrew distributions.  A candidate version is NOT approval to
    patch any file: `_adapter_matches` still audits source, worker permissions,
    active transforms, and the compiled Dashboard before any write.
    """
    match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)(?:-((?:rc|alpha|beta|preview|dev|insider)[.\-]?\d+)|"
        r"((?:rc|a|b|dev)\d+))?(?:\+[A-Za-z0-9._-]+)?",
        str(crew_version or ""), re.I,
    )
    if match is None:
        return False
    core = tuple(int(part) for part in match.group(1, 2, 3))
    return core >= _CREW_BASELINE


def _dashboard_discovery(target):
    # Reuse the audited presentation-only DOM transformer; this is not
    # an installation path for a prior Crew release.
    adapter = target if isinstance(target, Path) else getattr(target, "adapter_dir", None)
    if adapter is not None:
        asset = adapter.parents[2] / "src" / "kiro_byo" / "assets" / "crew11_ui_timing.js"
        if asset.is_file():
            return lambda crew_package: crew11_model_labels.discover_dashboard_patches(
                crew_package, timing_asset=asset
            )
    return crew11_model_labels.discover_dashboard_patches


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _activation_is_live(
    activation: dict[str, Any],
    installed_by_pid: int,
    app_build: str,
    *,
    current_pid: int | None = None,
    in_gateway: bool | None = None,
) -> bool:
    """Whether the Gateway serving this request has loaded the installed integration.

    Disk state alone proves nothing: Python keeps already-imported Crew modules
    (``dashboard.handlers.agents`` and friends) in memory, so an install written
    by the running Gateway is inert until that Gateway process is replaced.

    These routes are registered in-process by Kiro Crew's App bridge, so when we
    run inside the Gateway the only trustworthy proof is an activation record
    written by THIS very process, which must not be the process that performed
    the install.  Outside a Gateway (CLI, tests) fall back to the weaker
    "a different process activated this build" rule.
    """
    from .process_role import is_crew_gateway_process as _is_crew_gateway_process

    activation_pid = int(activation.get("pid") or 0)
    if not activation_pid or str(activation.get("kiro_byo_build") or "") != app_build:
        return False
    if installed_by_pid and activation_pid == installed_by_pid:
        return False
    if in_gateway is None:
        in_gateway = _is_crew_gateway_process()
    if in_gateway:
        pid = os.getpid() if current_pid is None else current_pid
        return activation_pid == pid and pid != installed_by_pid
    return True


def _sha_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest().lower()


def _normalized_sha(path: Path) -> str:
    return _sha_text(_normalized_text(path))


def _atomic_replace(dst: Path, writer: Callable[[Path], Any]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=dst.name + ".kiro-byo-", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(temp_name)
    try:
        writer(tmp)
        os.replace(tmp, dst)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_copy(src: Path, dst: Path) -> None:
    _atomic_replace(dst, lambda tmp: shutil.copy2(src, tmp))


def _atomic_write_text(text: str, dst: Path) -> None:
    _atomic_replace(dst, lambda tmp: tmp.write_text(text, encoding="utf-8", newline="\n"))


def _remove_pyc(py_file: Path) -> None:
    cache = py_file.parent / "__pycache__"
    if not cache.is_dir():
        return
    for candidate in cache.glob(f"{py_file.stem}.*.pyc"):
        try:
            candidate.unlink()
        except OSError:
            pass


def running_crew() -> tuple[str, Path, Path]:
    try:
        import kiro_crew  # type: ignore
    except Exception as exc:  # pragma: no cover - only absent outside Crew
        raise IntegrationError(f"KiroCrew runtime is unavailable: {exc}") from exc
    crew_package = Path(kiro_crew.__file__).resolve().parent
    site_packages = crew_package.parent
    version = str(getattr(kiro_crew, "__version__", "unknown") or "unknown")
    return version, crew_package, site_packages


def _compat_file_paths(compat: dict[str, Any], key: str) -> frozenset[str]:
    """Normalize an adapter's generated/created seam names consistently."""
    configured = compat.get(key)
    if not isinstance(configured, list):
        return frozenset()
    return frozenset(str(v).replace("\\", "/") for v in configured)


def _generated_files(compat: dict[str, Any]) -> frozenset[str]:
    return _compat_file_paths(compat, "generated_files")


def _created_files(compat: dict[str, Any]) -> frozenset[str]:
    return _compat_file_paths(compat, "created_files")


def _transformer_for(compat: dict[str, Any]):
    """Return the semantic transformer declared by one bundled Crew adapter."""
    name = str(compat.get("transformer") or "").strip().lower()
    if name != "adaptive12":
        raise IntegrationError(f"unsupported Crew 0.12 transformer: {name or '<missing>'}")
    module = crew12_patch
    return (
        frozenset(module.TARGET_FILES),
        module.PatchError,
        module.inspect,
        module.patch_text,
    )


def _converge_semantic_transform(
    relative: str,
    text: str,
    *,
    inspect_fn: Any,
    transform_fn: Any,
    transform_error: type[BaseException],
    max_passes: int = 4,
) -> str:
    """Render one generated Crew seam to an inspector-proven fixed point.

    ``upgradeable`` is deliberately weaker than ``patched``: it only proves the
    current bytes are Kiro BYO-owned and that another semantic rewrite is safe.
    A point-release migration may legitimately need more than one owned rewrite
    when two historical helper generations overlap.  The old installer applied
    exactly one rewrite and then verified only that those exact bytes reached
    disk.  That allowed it to return success even when the inspector immediately
    classified its own output as ``upgradeable`` again.

    Installation now proves the stronger invariant *before any Crew write*: the
    proposed output itself must inspect as ``patched``.  Unknown/foreign states
    still fail closed, and non-converging transforms are refused rather than
    guessed through indefinitely.
    """
    current = text
    for pass_index in range(max_passes + 1):
        try:
            inspection = inspect_fn(relative, current)
        except (OSError, transform_error) as exc:
            raise IntegrationError(f"cannot inspect transformed Crew seam {relative}: {exc}") from exc

        if inspection.state == "patched":
            return current
        if inspection.state not in {"clean", "upgradeable"}:
            raise IntegrationError(
                f"semantic transform for {relative} reached {inspection.state}: "
                f"{inspection.detail or 'transform anchors differ'}"
            )
        if pass_index >= max_passes:
            break

        try:
            rendered = transform_fn(relative, current)
        except transform_error as exc:
            raise IntegrationError(f"failed to transform {relative}: {exc}") from exc
        if rendered == current:
            raise IntegrationError(
                f"semantic transform for {relative} made no progress while state is "
                f"{inspection.state}"
            )
        current = rendered

    try:
        final = inspect_fn(relative, current)
    except (OSError, transform_error) as exc:
        raise IntegrationError(f"cannot inspect transformed Crew seam {relative}: {exc}") from exc
    raise IntegrationError(
        f"semantic transform for {relative} did not converge to patched after "
        f"{max_passes} passes (state={final.state}: "
        f"{final.detail or 'still requires another rewrite'})"
    )


def _safe_version_token(crew_version: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", crew_version)


def _backup_for(app_home: Path, crew_version: str, relative: str) -> Path:
    rel = str(relative).replace("\\", "/")
    if not rel.startswith(_PREFIX):
        raise IntegrationError(f"invalid Crew seam path: {rel}")
    return (
        app_home
        / "data"
        / "backups"
        / f"packaged-{_safe_version_token(crew_version)}"
        / rel[len(_PREFIX):]
    )


def _clean_recovery_text(
    *,
    app_home: Path | None,
    crew_version: str,
    relative: str,
    inspect_fn,
    transform_error,
) -> str | None:
    """Return a pristine backed-up seam when a previous Kiro BYO install stopped mid-flight.

    Older installs could write Crew seams one-by-one. If a later seam failed validation, earlier
    files could remain half-integrated. The installer never guesses through such a file;
    it only accepts a backup that the *current* semantic inspector proves is clean.
    """
    if app_home is None:
        return None
    backup = _backup_for(app_home, crew_version, relative)
    if not backup.is_file():
        return None
    try:
        text = _normalized_text(backup)
        inspection = inspect_fn(relative, text)
    except (OSError, transform_error):
        return None
    return text if inspection.state == "clean" else None


def _runtime_snapshot_created_file(
    app_home: Path | None, crew_version: str, relative: str
) -> Path | None:
    """Return the exact previously-installed owner file for a created seam.

    Created seams are Kiro BYO-owned files that do not exist in pristine Crew.
    During an app upgrade the packaged overlay legitimately changes, so comparing
    the live file only against the *new* overlay would misclassify our own previous
    version as foreign.  The integration-runtime snapshot records the exact bytes
    committed by the last successful install and is therefore the ownership proof.
    """
    if app_home is None:
        return None
    rel = str(relative).replace("\\", "/")
    snap = (
        app_home
        / "data"
        / "integration-runtime"
        / _safe_version_token(crew_version)
        / "overlay"
        / rel
    )
    return snap if snap.is_file() else None


def _legacy_byo_harness_is_owned(target: Path, relative: str) -> bool:
    """Recognize an earlier BYO-owned harness when its snapshot was lost.

    The 0.11 adapter's Mate 2.0 overlay changed this *created* module. A
    previous BYO version might have installed the old KiroByoHarness before
    snapshot tracking existed, or the user might have moved app data. Prove a
    distinctive Python owner-module shape rather than trusting a filename or a
    single comment. No other Crew path is eligible for this fallback.
    """
    if relative != "src/kiro_crew/acp/harness/kiro_byo.py":
        return False
    try:
        tree = ast.parse(_normalized_text(target), filename=str(target))
    except (OSError, SyntaxError, UnicodeError):
        return False
    if "Kiro BYO" not in (ast.get_docstring(tree) or ""):
        return False
    exported = False
    from_crew = False
    has_grant_projector = False
    harness = None
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.acp.harness._common":
            from_crew = any(alias.name == "MembershipHarness" for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_project_allowed_tools":
            has_grant_projector = True
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            try:
                exported = ast.literal_eval(node.value) == ["KiroByoHarness"]
            except (ValueError, TypeError, SyntaxError):
                pass
        elif isinstance(node, ast.ClassDef) and node.name == "KiroByoHarness":
            harness = node
    if not (from_crew and exported and has_grant_projector and harness):
        return False
    if len(harness.bases) != 1 or not isinstance(harness.bases[0], ast.Name) or harness.bases[0].id != "MembershipHarness":
        return False
    backend = any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "backend" for t in node.targets)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ACP_BACKEND_KIRO_BYO"
        for node in harness.body
    )
    methods = {
        node.name for node in harness.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return backend and {
        "resolve_spawn", "apply_spawn_env", "session_extras",
        "session_mcp_servers", "answer_request", "teardown",
    }.issubset(methods)


def _created_file_is_owned(
    *,
    target: Path,
    packaged_overlay: Path,
    app_home: Path | None,
    crew_version: str,
    relative: str,
) -> bool:
    """Prove that an existing create-only seam belongs to Kiro BYO.

    A file is owned when it matches either the current package overlay or the exact
    runtime snapshot from the previous successful install. Unknown bytes still fail
    closed, preserving the protection against overwriting another extension.
    """
    if not target.is_file() or not packaged_overlay.is_file():
        return False
    try:
        current_hash = _normalized_sha(target)
        if current_hash == _normalized_sha(packaged_overlay):
            return True
        previous = _runtime_snapshot_created_file(app_home, crew_version, relative)
        if previous is not None and current_hash == _normalized_sha(previous):
            return True
        # Recognize legacy BYO modules without assuming any other pre-existing
        # file is ours. The installer still snapshots and verifies replacement.
        return _legacy_byo_harness_is_owned(target, relative)
    except OSError:
        return False


def _regex_matches(pattern: str, value: str) -> bool:
    if not pattern:
        return True
    try:
        return re.fullmatch(pattern, value) is not None or re.search(pattern, value) is not None
    except re.error as exc:
        raise IntegrationError(f"invalid compatibility regex: {exc}") from exc


def _version_audited(compat: dict[str, Any], crew_version: str) -> bool:
    """Report audit confidence for explicit source-checked releases only.

    The candidate version regex is NOT evidence that a later prerelease was
    checked against the overlay. A semantic-compatible newer tag may install
    after validating its source; its UI audit flag remains false until the
    actual upstream tag is inspected and added to verified_versions.
    """
    verified = str(compat.get("verified_through") or "").strip()
    versions = compat.get("verified_versions")
    explicit = {verified} if verified else set()
    if isinstance(versions, list):
        explicit.update(value for value in versions if isinstance(value, str))
    if explicit:
        return crew_version in explicit
    pattern = str(compat.get("verified_family_regex") or "")
    return bool(pattern and _regex_matches(pattern, crew_version))


def _adapter_matches(
    adapter_dir: Path, crew_version: str, crew_package: Path, app_home: Path | None = None
) -> tuple[bool, list[str], dict[str, Any], dict[str, Any]]:
    manifest = _read_json(adapter_dir / "manifest.json")
    compat = _read_json(adapter_dir / "compatibility.json")
    if str(compat.get("transformer") or "") != "adaptive12":
        return False, ["unknown semantic transformer"], manifest, compat
    if not manifest or not compat:
        return False, ["manifest/compatibility missing"], manifest, compat
    # ``family_regex`` is only a broad candidate-version syntax gate.  For the
    # adaptive semantic adapter, source contracts below are authoritative: a
    # harmless Crew version bump must not become an incompatibility by itself.
    family = str(compat.get("family_regex") or "")
    if family:
        try:
            if not _regex_matches(family, crew_version):
                return False, [f"version {crew_version!r} is not a supported version syntax"], manifest, compat
        except IntegrationError:
            return False, ["invalid family_regex"], manifest, compat
    if not _crew12_version_candidate(crew_version):
        return False, [f"Crew {crew_version!r} is outside the KiroCrew 0.12 release-candidate/stable compatibility floor or has invalid syntax"], manifest, compat
    if (compat.get("semantic_unpinned_files") != compat.get("generated_files")
            or not isinstance(compat.get("semantic_unpinned_files"), list)):
        return False, ["0.12+ source preflight inventory changed"], manifest, compat
    minimum = str(compat.get("min_version") or "")
    if minimum:
        current_core = _version_core(crew_version)
        minimum_core = _version_core(minimum)
        if current_core is None or minimum_core is None:
            return False, ["invalid semantic version floor"], manifest, compat
        if current_core < minimum_core:
            return False, [f"version {crew_version!r} is older than supported floor {minimum}"], manifest, compat

    generated, created = _generated_files(compat), _created_files(compat)
    bad: list[str] = compatibility_contract_issues(compat, crew_package)
    bad.extend(rc6_contract_issues(crew_package))
    for raw in manifest.get("files") or []:
        relative = str(raw).replace("\\", "/")
        if not relative.startswith(_PREFIX):
            continue
        target = crew_package / relative[len(_PREFIX):]
        if _crew_seam_is_symlink(crew_package, target):
            bad.append(f"{relative} (symlinked Crew seam/parent is not patchable)")
            continue
        if relative in created:
            source = adapter_dir / "overlay" / relative
            if not source.is_file():
                bad.append(f"{relative} (missing created-file overlay)")
                continue
            if not target.exists():
                continue
            if not target.is_file():
                bad.append(f"{relative} (created-file target is not a file)")
                continue
            if _created_file_is_owned(
                target=target,
                packaged_overlay=source,
                app_home=app_home,
                crew_version=crew_version,
                relative=relative,
            ):
                continue
            bad.append(f"{relative} (foreign file occupies Kiro BYO-created path)")
            continue
        if not target.is_file():
            bad.append(f"{relative} (missing target)")
            continue

        if relative in generated:
            transform_files, transform_error, inspect_fn, transform_fn = _transformer_for(compat)
            if relative not in transform_files:
                bad.append(f"{relative} (no bundled transform)")
                continue
            try:
                current_text = _normalized_text(target)
                inspection = inspect_fn(relative, current_text)
            except (OSError, transform_error, SyntaxError) as exc:
                bad.append(f"{relative} ({exc})")
                continue
            if inspection.state in {"clean", "patched", "upgradeable"}:
                # Every 0.12 generated seam is semantically unpinned: the
                # compatibility inventory is required to equal generated_files.
                # Therefore there is no reachable source-hash shortcut. Prove
                # convergence and Python syntax for every variant, even if the
                # inspector initially reports it as already patched.
                try:
                    rendered = _converge_semantic_transform(
                        relative, current_text, inspect_fn=inspect_fn,
                        transform_fn=transform_fn, transform_error=transform_error,
                    )
                    compile(rendered, relative, "exec")
                except (IntegrationError, SyntaxError, ValueError) as exc:
                    bad.append(f"{relative} (unverified Crew source variant: {exc})")
                continue
            recovery = _clean_recovery_text(
                app_home=app_home,
                crew_version=crew_version,
                relative=relative,
                inspect_fn=inspect_fn,
                transform_error=transform_error,
            )
            if recovery is not None:
                # A known pristine backup makes a stale/partial prior Kiro BYO
                # patch repairable. The installer will preflight from the backup
                # and will not write anything until every seam has validated.
                continue
            bad.append(f"{relative} ({inspection.detail or 'transform anchors differ'})")
            continue

        bad.append(f"{relative} (unclassified Crew seam; expected generated_files or created_files)")

    # Dashboard model-picker DOM contracts are checked for every candidate.
    try:
        _dashboard_discovery(adapter_dir)(crew_package)
    except (crew_dashboard_patch.DashboardPatchError, OSError, UnicodeError) as exc:
        bad.append(f"src/kiro_crew/static/dist (candidate model-picker contract changed: {exc})")
    return not bad, bad, manifest, compat


def resolve_target(app_root: Path, app_home: Path | None = None) -> IntegrationTarget:
    crew_version, crew_package, site_packages = running_crew()
    if not _crew12_version_candidate(crew_version):
        raise IntegrationError(
            f"KiroCrew {crew_version} is not supported by this BYO 0.3.2 candidate: "
            "requires KiroCrew 0.12.0 RC/stable or newer and strict local source-contract preflight"
        )
    adapter_dir = app_root / "integration" / "kiro_crew" / "v0.12-plus"
    ok, reasons, manifest, compat = _adapter_matches(
        adapter_dir, crew_version, crew_package, app_home
    )
    if not ok:
        label = str(compat.get("baseline") or "KiroCrew 0.12+")
        raise IntegrationError(
            f"KiroCrew {crew_version} does not match the bundled {label} adapter: "
            + "; ".join(reasons[:8])
        )
    return IntegrationTarget(
        crew_version, crew_package, site_packages, adapter_dir,
        str(compat.get("baseline") or "KiroCrew 0.12+"), manifest, compat,
    )


def _legacy_state(site_packages: Path) -> dict[str, Any]:
    return _read_json(site_packages / "kiro_byo" / "_bootstrap_state.json")


def legacy_home(site_packages: Path) -> Path | None:
    raw = _legacy_state(site_packages).get("kiro_byo_home")
    if isinstance(raw, str) and raw.strip():
        p = Path(raw.strip()).expanduser()
        return p if p.exists() else None
    return None


def migrate_legacy_data(app_home: Path, site_packages: Path) -> Path | None:
    """Import an existing manual-install home into the App data directory once."""
    old = legacy_home(site_packages)
    if old is None or old.resolve() == app_home.resolve():
        return old
    app_home.mkdir(parents=True, exist_ok=True)
    for rel in ("config", "data/sessions", "data/telemetry", "data/runtime_trace", "data/audit", "data/backups"):
        src = old / rel
        dst = app_home / rel
        if not src.exists() or dst.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return old


def _app_version(app_root: Path) -> str:
    """Return the App package version used to judge runtime freshness.

    The Kiro BYO ACP runtime is copied into KiroCrew's bundled Python
    site-packages during integration install. Updating the App bundle alone does
    not replace that copied runtime, so integration readiness must compare the
    live runtime marker with the current App version instead of merely checking
    that a ``kiro_byo`` directory exists.
    """
    try:
        value = (app_root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    return value


def _app_build_id(app_root: Path) -> str:
    """Return an internal fingerprint for same-version App updates.

    Hash the runtime/integration sources so a copied runtime from an earlier
    build of the same public release is still detected as stale.
    """
    roots = [app_root / "src" / "kiro_byo", app_root / "integration" / "kiro_crew"]
    digest = hashlib.sha256()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            digest.update(path.relative_to(app_root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                continue
            digest.update(b"\0")
    return digest.hexdigest().lower()


def _runtime_matches_app(
    runtime_version: str,
    runtime_build: str,
    app_version: str,
    app_build: str,
) -> bool:
    """Return whether the copied Crew runtime belongs to this App build.

    The branded 0.1.x line always records a build fingerprint. Missing build
    metadata therefore means a stale/pre-fingerprint runtime and must trigger a
    reinstall/restart instead of being accepted solely because the public
    version string matches. Historical pre-brand App versions keep their
    version-only compatibility behavior for old integration-state records.
    """
    if not app_version or runtime_version != app_version:
        return False
    if runtime_build:
        return runtime_build == app_build
    return not app_version.startswith("0.1.")


def quick_integration_status(app_root: Path, app_home: Path) -> dict[str, Any]:
    """Return the cheap App-shell integration state without a semantic audit."""
    try:
        crew_version, _crew_package, site_packages = running_crew()
    except IntegrationError as exc:
        return {
            "supported": False, "crew_version": "unknown", "installed": False,
            "active": False, "restart_required": False, "repairable": False,
            "runtime_version": "", "app_version": _app_version(app_root),
            "runtime_current": False, "app_home_bound": False,
            "has_recorded_state": False, "error": str(exc),
        }

    recorded = _read_json(app_home / "data" / "integration.json")
    runtime_state = _read_json(site_packages / "kiro_byo" / "_bootstrap_state.json")
    activation = _read_json(app_home / "data" / "activation.json")
    app_version = _app_version(app_root)
    app_build = _app_build_id(app_root)
    runtime_version = str(runtime_state.get("kiro_byo_version") or "")
    runtime_build = str(runtime_state.get("kiro_byo_build") or "")
    runtime_current = _runtime_matches_app(
        runtime_version, runtime_build, app_version, app_build
    )
    app_home_bound = str(runtime_state.get("kiro_byo_home") or "") == str(app_home)
    disk_installed = bool(recorded) and runtime_current and app_home_bound and str(recorded.get("detected_version") or "") == crew_version

    installed_by_pid = int(recorded.get("installed_by_pid") or 0)
    active = bool(
        disk_installed
        and _activation_is_live(activation, installed_by_pid, app_build)
    )
    return {
        "supported": True,
        "crew_version": crew_version,
        "baseline": str(recorded.get("baseline") or ""),
        "strategy": "adaptive_semantic",
        "installed": disk_installed,
        "active": active,
        "restart_required": bool(disk_installed and not active),
        "repairable": False,
        "repairable_files": [],
        "pending_files": [],
        "runtime_version": runtime_version,
        "app_version": app_version,
        "runtime_current": runtime_current,
        "app_home_bound": app_home_bound,
        "has_recorded_state": bool(recorded),
        "error": "",
    }


def integration_status(app_root: Path, app_home: Path) -> dict[str, Any]:
    recorded_state = _read_json(app_home / "data" / "integration.json")
    try:
        target = resolve_target(app_root, app_home)
        current: list[bool] = []
        repairable_files: list[str] = []
        pending_files: list[dict[str, str]] = []
        for raw in target.manifest.get("files") or []:
            rel = str(raw).replace("\\", "/")
            if not rel.startswith(_PREFIX):
                continue
            dest = target.crew_package / rel[len(_PREFIX):]
            if rel in target.created_files:
                source = target.overlay_root / rel
                matched = source.is_file() and dest.is_file() and _normalized_sha(source) == _normalized_sha(dest)
                current.append(matched)
                if not matched:
                    detail = "not installed" if not dest.exists() else "created-file content differs"
                    pending_files.append({"file": rel, "state": "missing" if not dest.exists() else "changed", "detail": detail})
                continue
            if not dest.is_file():
                current.append(False)
                pending_files.append({"file": rel, "state": "missing", "detail": "target missing"})
                continue
            if rel in target.generated_files:
                _files, transform_error, inspect_fn, _transform_fn = _transformer_for(
                    target.compatibility
                )
                try:
                    inspection = inspect_fn(rel, _normalized_text(dest))
                    patched = inspection.state == "patched"
                    current.append(patched)
                    if not patched:
                        pending_files.append({
                            "file": rel,
                            "state": inspection.state,
                            "detail": inspection.detail or "semantic integration not present",
                        })
                    if inspection.state == "incompatible":
                        recovery = _clean_recovery_text(
                            app_home=app_home,
                            crew_version=target.crew_version,
                            relative=rel,
                            inspect_fn=inspect_fn,
                            transform_error=transform_error,
                        )
                        if recovery is not None:
                            repairable_files.append(rel)
                except (OSError, transform_error) as exc:
                    current.append(False)
                    pending_files.append({"file": rel, "state": "error", "detail": str(exc)})
                continue
            current.append(False)
            pending_files.append({
                "file": rel,
                "state": "unclassified",
                "detail": "Crew seams must be semantic-generated or Kiro BYO-created",
            })

        dashboard_current = True
        dashboard_dist = target.crew_package / "static" / "dist"
        if dashboard_dist.is_dir():
            try:
                dashboard_probe = _dashboard_discovery(target)(target.crew_package)
                dashboard_current = bool(dashboard_probe) and all(not item.changed for item in dashboard_probe)
                for item in dashboard_probe:
                    if item.changed:
                        rel = _PREFIX + item.path.relative_to(target.crew_package).as_posix()
                        pending_files.append({
                            "file": rel,
                            "state": "dashboard-unpatched",
                            "detail": "dashboard Type/model safety guard is missing or stale",
                        })
            except crew_dashboard_patch.DashboardPatchError as exc:
                dashboard_current = False
                pending_files.append({
                    "file": "src/kiro_crew/static/dist",
                    "state": "dashboard-incompatible",
                    "detail": str(exc),
                })

        runtime = target.site_packages / "kiro_byo"
        state = _read_json(runtime / "_bootstrap_state.json")
        runtime_version = str(state.get("kiro_byo_version") or "")
        runtime_build = str(state.get("kiro_byo_build") or "")
        app_version = _app_version(app_root)
        app_build = _app_build_id(app_root)
        runtime_current = _runtime_matches_app(
            runtime_version, runtime_build, app_version, app_build
        )
        seams_current = bool(current) and all(current) and dashboard_current
        runtime_present = runtime.is_dir()
        installed = seams_current and runtime_present and runtime_current
        activation = _read_json(app_home / "data" / "activation.json")
        installed_by_pid = int(recorded_state.get("installed_by_pid") or 0)
        active = bool(
            installed
            and _activation_is_live(activation, installed_by_pid, app_build)
        )
        if seams_current and runtime_present and not runtime_current:
            pending_files.append({
                "file": "site-packages/kiro_byo",
                "state": "stale-runtime",
                "detail": (
                    f"runtime {runtime_version or '<unknown>'} / build "
                    f"{runtime_build[:12] or '<unknown>'} does not match App "
                    f"{app_version or '<unknown>'} / build {app_build[:12]}; "
                    "install/update integration"
                ),
            })
        return {
            "supported": True,
            "crew_version": target.crew_version,
            "baseline": target.baseline,
            "strategy": str(target.compatibility.get("mode") or "verified_overlay"),
            "transformer": str(target.compatibility.get("transformer") or ""),
            "verified_through": str(target.compatibility.get("verified_through") or ""),
            "verified_commit": str(target.compatibility.get("verified_commit") or ""),
            "version_audited": _version_audited(target.compatibility, target.crew_version),
            "source_contract_checked_version": target.crew_version,
            "source_contract_status": "passed" if target.crew_version else "unknown",
            "source_contract_seams": len(target.manifest.get("files") or []),
            "native_registration": inspect_native_registration(target.crew_package),
            "installed": installed,
            "active": active,
            "restart_required": bool(installed and not active),
            "repairable": bool(repairable_files),
            "repairable_files": repairable_files,
            "pending_files": pending_files,
            "runtime_version": runtime_version,
            "app_version": app_version,
            "runtime_current": runtime_current,
            "app_home_bound": str(state.get("kiro_byo_home") or "") == str(app_home),
            "has_recorded_state": bool(recorded_state),
            "error": "",
        }
    except IntegrationError as exc:
        crew_version = "unknown"
        crew_package_for_probe = None
        try:
            crew_version, crew_package_for_probe, _sp = running_crew()
        except IntegrationError:
            pass
        return {
            "supported": False,
            "crew_version": crew_version,
            "baseline": "",
            "strategy": "",
            "transformer": "",
            "verified_through": "",
            "verified_commit": "",
            "version_audited": False,
            "source_contract_status": "failed",
            "source_contract_checked_version": crew_version,
            "source_contract_seams": 0,
            "native_registration": (inspect_native_registration(crew_package_for_probe)
                                    if crew_package_for_probe is not None else
                                    {"status": "source_unavailable", "native_integration_verified": False}),
            "installed": False,
            "repairable": False,
            "repairable_files": [],
            "pending_files": [],
            "runtime_version": "",
            "app_version": _app_version(app_root),
            "runtime_current": False,
            "app_home_bound": False,
            "restart_required": False,
            "has_recorded_state": bool(recorded_state),
            "error": str(exc),
        }


def _write_runtime_snapshot(
    target: IntegrationTarget,
    app_home: Path,
    outputs: dict[str, str | Path],
    base_hashes: dict[str, str],
    dashboard_assets: list[str] | tuple[str, ...] = (),
) -> tuple[Path, Path, Path]:
    """Persist the exact installed overlay so startup self-heal is deterministic."""
    safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", target.crew_version)
    snap = app_home / "data" / "integration-runtime" / safe_version
    overlay_root = snap / "overlay"
    if snap.exists():
        shutil.rmtree(snap)
    overlay_root.mkdir(parents=True, exist_ok=True)
    for rel, output in outputs.items():
        dst = overlay_root / rel
        if isinstance(output, Path):
            _atomic_copy(output, dst)
        else:
            _atomic_write_text(output, dst)

    manifest_path = snap / "manifest.json"
    compatibility_path = snap / "compatibility.json"
    manifest_path.write_text(
        json.dumps(target.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    runtime_compat = {
        "family_regex": str(target.compatibility.get("family_regex") or f"^{re.escape(target.crew_version)}$"),
        "verified_family_regex": str(target.compatibility.get("verified_family_regex") or ""),
        "min_version": str(target.compatibility.get("min_version") or ""),
        "baseline": target.baseline,
        "hash_mode": "normalized_utf8_lf",
        "mode": str(target.compatibility.get("mode") or ""),
        "min_insider": target.compatibility.get("min_insider"),
        "base_sha256": base_hashes,
        "generated_files": sorted(target.generated_files),
        "created_files": sorted(target.created_files),
        "dashboard_assets": sorted(str(v).replace("\\", "/") for v in dashboard_assets),
        "transformer": str(target.compatibility.get("transformer") or ""),
        "verified_through": str(target.compatibility.get("verified_through") or ""),
        "verified_commit": str(target.compatibility.get("verified_commit") or ""),
        "verified_at": str(target.compatibility.get("verified_at") or ""),
        "required_contract_symbols": target.compatibility.get("required_contract_symbols") or {},
        "required_contract_exports": target.compatibility.get("required_contract_exports") or {},
    }
    compatibility_path.write_text(
        json.dumps(runtime_compat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return overlay_root, manifest_path, compatibility_path


def _plan_created_seam(
    target: IntegrationTarget, app_home: Path, rel: str, dst: Path,
    persistent_backup: Path,
) -> tuple[dict[str, Any], Path]:
    """A created (BYO-owned) seam must never overwrite a foreign Crew file."""
    src = target.overlay_root / rel
    if not src.is_file():
        raise IntegrationError(f"missing created-file overlay: {rel}")
    existed_before = dst.exists()
    if existed_before:
        if not dst.is_file():
            raise IntegrationError(f"created-file target is not a file: {rel}")
        current_hash = _normalized_sha(dst)
        if not _created_file_is_owned(
            target=dst, packaged_overlay=src, app_home=app_home,
            crew_version=target.crew_version, relative=rel,
        ):
            raise IntegrationError(
                f"refusing to replace foreign file at Kiro BYO-created path: {rel}"
            )
    else:
        current_hash = ""
    return {
        "rel": rel,
        "dst": dst,
        "current_hash": current_hash,
        "output_path": src,
        "backup_text": None,
        "persistent_backup": persistent_backup,
        "existed_before": existed_before,
        "created_file": True,
    }, src


def _resolve_owned_generated_baseline(
    *, app_home: Path, safe_version: str, rel: str,
    persistent_backup: Path, inspect_fn: Any, transform_error: type[Exception],
) -> tuple[str, bool]:
    """Check pristine backup or recover its recorded hash; never guess a baseline."""
    if persistent_backup.is_file():
        try:
            pristine_text = _normalized_text(persistent_backup)
            backup_inspection = inspect_fn(rel, pristine_text)
        except (OSError, transform_error) as exc:
            raise IntegrationError(
                f"cannot validate pristine backup for {rel}: {exc}"
            ) from exc
        if backup_inspection.state != "clean":
            raise IntegrationError(
                f"pristine backup for {rel} is not clean; restore/update Kiro Crew once"
            )
        return _sha_text(pristine_text), False
    previous = _read_json(
        app_home / "data" / "integration-runtime" / safe_version / "compatibility.json"
    )
    previous_base = (
        previous.get("base_sha256")
        if isinstance(previous.get("base_sha256"), dict)
        else {}
    )
    base_hash = str(previous_base.get(rel) or "")
    return base_hash, not bool(base_hash)


def _plan_generated_seam(
    target: IntegrationTarget, app_home: Path, safe_version: str,
    rel: str, dst: Path, persistent_backup: Path,
) -> tuple[dict[str, Any], str, str, bool, bool]:
    """Preflight one semantic generated seam and declare its expected output.

    Return (plan, output, pristine hash, partial recovered, missing baseline).
    All transformation remains fail-closed, including partial-write recovery.
    """
    if not dst.is_file():
        raise IntegrationError(f"missing Crew seam: {rel}")
    current_text = _normalized_text(dst)
    current_hash = _sha_text(current_text)
    transform_files, transform_error, inspect_fn, transform_fn = _transformer_for(
        target.compatibility
    )
    if rel not in transform_files:
        raise IntegrationError(f"no bundled semantic transformer for Crew seam: {rel}")
    try:
        inspection = inspect_fn(rel, current_text)
    except (OSError, transform_error) as exc:
        raise IntegrationError(f"cannot inspect changed Crew seam {rel}: {exc}") from exc

    backup_text: str | None = None
    recovered = False
    missing_baseline = False
    if inspection.state == "clean":
        pristine_text = current_text
        base_hash = current_hash
        patched_text = _converge_semantic_transform(
            rel, pristine_text, inspect_fn=inspect_fn, transform_fn=transform_fn,
            transform_error=transform_error,
        )
        if not persistent_backup.exists():
            backup_text = pristine_text
    elif inspection.state in {"patched", "upgradeable"}:
        # Keep upgrading owned seams even when the pristine backup went missing.
        # Destructive uninstall stays fail-closed if the baseline is unproven.
        if inspection.state == "upgradeable":
            patched_text = _converge_semantic_transform(
                rel, current_text, inspect_fn=inspect_fn, transform_fn=transform_fn,
                transform_error=transform_error,
            )
        else:
            patched_text = current_text
        base_hash, missing_baseline = _resolve_owned_generated_baseline(
            app_home=app_home, safe_version=safe_version, rel=rel,
            persistent_backup=persistent_backup, inspect_fn=inspect_fn,
            transform_error=transform_error,
        )
    else:
        # Never transform a partial/foreign seam directly. Recover only from
        # a baseline the semantic inspector proves pristine.
        recovery = _clean_recovery_text(
            app_home=app_home, crew_version=target.crew_version,
            relative=rel, inspect_fn=inspect_fn, transform_error=transform_error,
        )
        if recovery is None:
            raise IntegrationError(
                f"refusing to transform changed Crew seam: {rel} "
                f"({inspection.detail or 'transform anchors differ'})"
            )
        pristine_text = recovery
        base_hash = _sha_text(pristine_text)
        patched_text = _converge_semantic_transform(
            rel, pristine_text, inspect_fn=inspect_fn, transform_fn=transform_fn,
            transform_error=transform_error,
        )
        recovered = True

    return {
        "rel": rel,
        "dst": dst,
        "current_hash": current_hash,
        "output_text": patched_text,
        "backup_text": backup_text,
        "persistent_backup": persistent_backup,
        "existed_before": True,
        "created_file": False,
    }, patched_text, base_hash, recovered, missing_baseline


def _prepare_seam_install_plans(
    target: IntegrationTarget, app_home: Path, safe_version: str,
) -> tuple[
    list[dict[str, Any]], dict[str, str | Path], dict[str, str], list[str], list[str]
]:
    """Plan the Crew source writes without changing any Crew files."""
    plans: list[dict[str, Any]] = []
    installed_outputs: dict[str, str | Path] = {}
    runtime_base_hashes: dict[str, str] = {}
    recovered_files: list[str] = []
    baseline_missing_files: list[str] = []

    for raw in target.manifest.get("files") or []:
        rel = str(raw).replace("\\", "/")
        if not rel.startswith(_PREFIX):
            continue
        dst = target.crew_package / rel[len(_PREFIX):]
        persistent_backup = _backup_for(app_home, target.crew_version, rel)
        if rel in target.created_files:
            plan, output = _plan_created_seam(
                target, app_home, rel, dst, persistent_backup,
            )
        elif rel in target.generated_files:
            plan, output, base_hash, recovered, missing_baseline = _plan_generated_seam(
                target, app_home, safe_version, rel, dst, persistent_backup,
            )
            runtime_base_hashes[rel] = base_hash
            if recovered:
                recovered_files.append(rel)
            if missing_baseline:
                baseline_missing_files.append(rel)
        else:
            # Preserve the original fail-closed exception for unclassified seams.
            if not dst.is_file():
                raise IntegrationError(f"missing Crew seam: {rel}")
            raise IntegrationError(
                f"unclassified Crew 0.10 seam in manifest: {rel}; "
                "expected generated_files or created_files"
            )
        installed_outputs[rel] = output
        plans.append(plan)

    plans.extend(_plan_retired_generated_seams(target, app_home, safe_version))
    return plans, installed_outputs, runtime_base_hashes, recovered_files, baseline_missing_files


def _append_dashboard_install_plans(
    target: IntegrationTarget,
    app_home: Path,
    plans: list[dict[str, Any]],
    installed_outputs: dict[str, str | Path],
    runtime_base_hashes: dict[str, str],
    baseline_missing_files: list[str],
) -> tuple[list[str], Path]:
    """Validate and plan dashboard guard patches without mutating assets."""
    dashboard_assets: list[str] = []
    # KiroCrew 0.10 shares one model catalogue within a page lifetime. The
    # dashboard guard keeps Chat and non-Chat lifetimes surface-pure and avoids
    # patching content-hashed Vite/Rollup helpers. Synthetic/test Crew packages
    # may omit the dist.
    dashboard_dist = target.crew_package / "static" / "dist"
    # Crew 0.12 uses a display-only HTML guard verified against the
    # installed data-model-id and composer chip DOM contracts. No JS chunks or
    # ACP payloads are rewritten.
    if dashboard_dist.is_dir():
        try:
            asset_patches = _dashboard_discovery(target)(target.crew_package)
        except crew_dashboard_patch.DashboardPatchError as exc:
            raise IntegrationError(f"KiroCrew dashboard model-picker contract changed: {exc}") from exc
        for asset in asset_patches:
            rel = _PREFIX + asset.path.relative_to(target.crew_package).as_posix()
            persistent_backup = _backup_for(app_home, target.crew_version, rel)
            current_text = _normalized_text(asset.path)
            if current_text != asset.original.replace("\r\n", "\n").replace("\r", "\n"):
                raise IntegrationError(f"dashboard asset changed during patch discovery: {rel}; retry")
            current_hash = _sha_text(current_text)
            backup_text = None
            if asset.changed and not persistent_backup.exists():
                backup_text = current_text
            elif not asset.changed and not persistent_backup.exists():
                baseline_missing_files.append(rel)
            installed_outputs[rel] = asset.patched
            runtime_base_hashes[rel] = (
                _normalized_sha(persistent_backup) if persistent_backup.is_file() else ""
            )
            dashboard_assets.append(rel)
            plans.append({
                "rel": rel,
                "dst": asset.path,
                "current_hash": current_hash,
                "output_text": asset.patched,
                "backup_text": backup_text,
                "persistent_backup": persistent_backup,
                "existed_before": True,
                "created_file": False,
                "dashboard_asset": True,
            })

    return dashboard_assets, dashboard_dist


def _verify_installed_seams(
    target: IntegrationTarget,
    plans: list[dict[str, Any]],
    dashboard_dist: Path,
) -> None:
    """Read-only commit gate: exact output hashes and generated semantic closure.

    Failures raise IntegrationError inside the install transaction, preserving
    the caller's original rollback, snapshot and fail-closed behavior.
    """
    # Verify the exact committed bytes before installing/updating the ACP
    # runtime.  Earlier Crew builds could report "written" even when a host-side
    # replace/filter left one Crew seam in the old partial state.  A
    # successful integration transaction therefore means every planned seam is
    # already the exact output that passed preflight.
    commit_mismatches: list[str] = []
    for plan in plans:
        dst = plan["dst"]
        if "output_text" in plan:
            expected = _sha_text(str(plan["output_text"]))
            actual = _normalized_sha(dst)
        else:
            expected = _normalized_sha(Path(plan["output_path"]))
            actual = _normalized_sha(dst)
        if actual != expected:
            commit_mismatches.append(plan["rel"])
    if commit_mismatches:
        raise IntegrationError(
            "integration commit verification failed for: "
            + ", ".join(commit_mismatches)
        )

    # Byte equality only proves that the installer wrote what it planned.
    # The stronger integration invariant is semantic closure: every
    # generated seam must already inspect as ``patched`` and the dashboard
    # guard must need no further rewrite.  This catches the exact class of
    # failure where a one-pass owned upgrade writes successfully but the
    # immediate post-install audit still reports ``upgradeable`` or
    # ``dashboard-unpatched``.
    semantic_mismatches: list[str] = []
    _semantic_files, semantic_error, semantic_inspect, _semantic_transform = (
        _transformer_for(target.compatibility)
    )
    for plan in plans:
        rel = str(plan["rel"])
        if rel not in target.generated_files:
            continue
        try:
            inspection = semantic_inspect(rel, _normalized_text(plan["dst"]))
        except (OSError, semantic_error) as exc:
            semantic_mismatches.append(f"{rel}: error ({exc})")
            continue
        if inspection.state != "patched":
            semantic_mismatches.append(
                f"{rel}: {inspection.state} "
                f"({inspection.detail or 'semantic integration not closed'})"
            )

    if dashboard_dist.is_dir():
        try:
            committed_dashboard = _dashboard_discovery(target)(
                target.crew_package
            )
        except crew_dashboard_patch.DashboardPatchError as exc:
            semantic_mismatches.append(f"src/kiro_crew/static/dist: {exc}")
        else:
            for asset in committed_dashboard:
                if asset.changed:
                    rel = _PREFIX + asset.path.relative_to(target.crew_package).as_posix()
                    semantic_mismatches.append(
                        f"{rel}: dashboard guard still requires another rewrite"
                    )

    # Semantic patch convergence alone does not prove that Python import names
    # resolve. Verify cross-file BYO exports before accepting a committed install.
    installed_generated = {
        str(plan["rel"]): _normalized_text(plan["dst"])
        for plan in plans if str(plan["rel"]) in target.generated_files
    }
    semantic_mismatches.extend(byo_import_closure_issues(installed_generated))

    if semantic_mismatches:
        raise IntegrationError(
            "integration semantic commit verification failed for: "
            + "; ".join(semantic_mismatches)
        )


def _commit_runtime_and_install_metadata(
    *,
    target: IntegrationTarget,
    app_root: Path,
    app_home: Path,
    version: str,
    backup_root: Path,
    effective_overlay: Path,
    effective_manifest: Path,
    effective_compat: Path,
    dashboard_assets: list[str],
    baseline_missing_files: list[str],
    recovered_files: list[str],
) -> None:
    """Copy the ACP runtime and persist install metadata inside the caller's transaction.

    Do not catch errors here: install_from_app must roll back Crew files,
    runtime package, bootstrap pth, integration metadata and runtime snapshot
    together if any step fails.
    """
    runtime_dst = target.site_packages / "kiro_byo"
    pth_path = target.site_packages / "kiro_byo_bootstrap.pth"
    integration_path = app_home / "data" / "integration.json"
    # Install the ACP runtime next to KiroCrew's Python. Copy files in-place
    # so Windows does not need to rename a package directory another child
    # may have open.
    runtime_src = app_root / "src" / "kiro_byo"
    runtime_dst.mkdir(parents=True, exist_ok=True)
    runtime_files = {
        src.relative_to(runtime_src)
        for src in runtime_src.rglob("*")
        if src.is_file() and "__pycache__" not in src.parts and src.suffix != ".pyc"
    }
    # Same-version reinstalls are supported. Remove files previously
    # owned by the copied runtime that no longer exist in this build so
    # stale modules cannot survive indefinitely. Bootstrap state is
    # generated below and therefore excluded from source membership.
    for old in sorted(runtime_dst.rglob("*"), reverse=True):
        if not old.is_file() or "__pycache__" in old.parts or old.suffix == ".pyc":
            continue
        rel = old.relative_to(runtime_dst)
        if rel == Path("_bootstrap_state.json"):
            continue
        if rel not in runtime_files:
            old.unlink(missing_ok=True)
    for rel in sorted(runtime_files):
        _atomic_copy(runtime_src / rel, runtime_dst / rel)

    app_build = _app_build_id(app_root)
    state = {
        "kiro_byo_version": version,
        "kiro_byo_build": app_build,
        "kiro_byo_home": str(app_home),
        "kiro_crew_home": "",
        "crew_package": str(target.crew_package),
        "overlay_version": target.adapter_dir.name,
        "overlay_root": str(effective_overlay),
        "manifest_path": str(effective_manifest),
        "compatibility_path": str(effective_compat),
        "detected_version": target.crew_version,
        "installed_via": "kirocrew-app",
    }
    _atomic_write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        runtime_dst / "_bootstrap_state.json",
    )
    _atomic_write_text("import kiro_byo.bootstrap_patch\n", pth_path)

    integration = {
        "kiro_byo_version": version,
        "kiro_byo_build": app_build,
        "installed_by_pid": os.getpid(),
        "baseline": target.baseline,
        "detected_version": target.crew_version,
        "overlay_version": target.adapter_dir.name,
        "generated_files": sorted(target.generated_files),
        "created_files": sorted(target.created_files),
        "dashboard_assets": sorted(dashboard_assets),
        "baseline_missing_files": sorted(set(baseline_missing_files)),
        "backup_root": str(backup_root),
        "runtime_overlay_root": str(effective_overlay),
        "mode": "kirocrew-app",
        "crew_package": str(target.crew_package),
        "site_packages": str(target.site_packages),
        "kiro_byo_home": str(app_home),
        "recovered_partial_files": recovered_files,
        "installed_at": datetime.now(timezone.utc).astimezone().isoformat(),
    }
    state_path = integration_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        json.dumps(integration, ensure_ascii=False, indent=2) + "\n", state_path
    )


def _persist_install_baselines(plans: list[dict[str, Any]]) -> None:
    """Persist pristine source baselines only after every seam passes preflight."""
    for plan in plans:
        backup_text = plan.get("backup_text")
        backup = plan["persistent_backup"]
        if backup_text is not None and not backup.exists():
            _atomic_write_text(str(backup_text), backup)


def _snapshot_transaction_path(dst: Path, snap: Path, *, directory: bool = False) -> bool:
    existed = dst.is_dir() if directory else dst.is_file()
    if existed:
        if directory:
            shutil.copytree(dst, snap)
        else:
            shutil.copy2(dst, snap)
    return existed


def _restore_transaction_path(dst: Path, snap: Path, existed: bool, *, directory: bool = False) -> None:
    """Restore only a transaction-owned file/directory to its pre-write state."""
    if directory:
        if dst.exists():
            shutil.rmtree(dst)
        if existed:
            shutil.copytree(snap, dst)
    elif existed:
        _atomic_copy(snap, dst)
    else:
        dst.unlink(missing_ok=True)


def _apply_install_plan(plan: dict[str, Any], touched_seams: list[dict[str, Any]]) -> None:
    """Commit one preflighted seam; record ownership *before* any write for rollback."""
    dst = plan["dst"]
    if "output_text" in plan:
        output = str(plan["output_text"])
        if _sha_text(_normalized_text(dst)) == _sha_text(output):
            return
        touched_seams.append(plan)
        _atomic_write_text(output, dst)
    else:
        src = Path(plan["output_path"])
        if dst.is_file() and _normalized_sha(dst) == _normalized_sha(src):
            return
        touched_seams.append(plan)
        _atomic_copy(src, dst)
    _remove_pyc(dst)


def install_from_app(app_root: Path, app_home: Path, *, version: str) -> dict[str, Any]:
    """Install/update the ACP integration transactionally.

    The transactional installer has two invariants that older installers did not have:

    * **preflight first** -- every Crew seam must be proven clean/patched (or
      recoverable from a pristine Kiro BYO backup) before the first Crew source
      file is changed;
    * **rollback on write failure** -- if any later write/runtime step fails,
      every Crew source touched by this transaction is restored to the exact
      bytes present when the transaction started.

    This also repairs the specific partial-write failure mode where an early seam was
    written and a later semantic check aborted, leaving a ``partial/foreign
    Kiro BYO patch`` behind.
    """
    target = resolve_target(app_root, app_home)
    app_home.mkdir(parents=True, exist_ok=True)
    old_home = migrate_legacy_data(app_home, target.site_packages)

    safe_version = _safe_version_token(target.crew_version)
    backup_root = app_home / "data" / "backups" / f"packaged-{safe_version}"
    if old_home is not None:
        old_backup = old_home / "data" / "backups" / f"packaged-{safe_version}"
        if old_backup.is_dir() and not backup_root.exists():
            shutil.copytree(old_backup, backup_root)

    # Pure preflight/plan construction stays outside the write/rollback transaction.
    # The source, dashboard contract, and output bytes are verified before touching Crew.
    (
        plans, installed_outputs, runtime_base_hashes,
        recovered_files, baseline_missing_files,
    ) = _prepare_seam_install_plans(target, app_home, safe_version)
    dashboard_assets, dashboard_dist = _append_dashboard_install_plans(
        target, app_home, plans, installed_outputs,
        runtime_base_hashes, baseline_missing_files,
    )

    # Before the first write, audit the imports that will actually be installed.
    # This would have rejected the 0.3.3 mirror-registry Gateway crash.
    generated_source = {
        str(plan["rel"]): str(plan["output_text"])
        for plan in plans if str(plan["rel"]) in target.generated_files
        and "output_text" in plan
    }
    closure_issues = byo_import_closure_issues(generated_source)
    if closure_issues:
        raise IntegrationError(
            "Crew BYO import closure preflight failed: " + "; ".join(closure_issues)
        )

    _persist_install_baselines(plans)

    # Snapshot the exact pre-transaction Crew files. If anything below fails,
    # restore these bytes so a failed update cannot manufacture another partial
    # integration state.
    txn_parent = app_home / "data" / "integration-transactions"
    txn_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kiro-byo-", dir=str(txn_parent)) as td:
        txn_root = Path(td)
        for plan in plans:
            snap = txn_root / plan["rel"][len(_PREFIX):]
            snap.parent.mkdir(parents=True, exist_ok=True)
            if plan.get("existed_before", True):
                shutil.copy2(plan["dst"], snap)
                plan["txn_backup"] = snap
            else:
                plan["txn_backup"] = None

        # Snapshot runtime and integration metadata before any Crew mutation.
        # Keep one inventory for both capture and rollback: no copied surface
        # may silently fall out of the transaction when a new one is added.
        surface_snapshots = [
            (label, dst, snap, _snapshot_transaction_path(dst, snap, directory=is_dir), is_dir)
            for label, dst, snap, is_dir in (
                ("runtime package", target.site_packages / "kiro_byo", txn_root / "__runtime_package__", True),
                ("bootstrap pth", target.site_packages / "kiro_byo_bootstrap.pth", txn_root / "__bootstrap.pth", False),
                ("integration state", app_home / "data" / "integration.json", txn_root / "__integration.json", False),
                ("runtime snapshot", app_home / "data" / "integration-runtime" / safe_version, txn_root / "__integration_runtime__", True),
            )
        ]

        touched_seams: list[dict[str, Any]] = []
        touched_runtime = False
        try:
            # Detect an updater/race between preflight and commit.
            for plan in plans:
                issue = _install_plan_precommit_issue(plan, target.crew_package, _normalized_sha)
                if issue:
                    raise IntegrationError(issue)

            for plan in plans:
                _apply_install_plan(plan, touched_seams)

            # Both byte identity and semantic closure are required before installing ACP.
            _verify_installed_seams(target, plans, dashboard_dist)

            touched_runtime = True
            effective_overlay, effective_manifest, effective_compat = _write_runtime_snapshot(
                target, app_home, installed_outputs, runtime_base_hashes, dashboard_assets
            )

            _commit_runtime_and_install_metadata(
                target=target, app_root=app_root, app_home=app_home,
                version=version, backup_root=backup_root,
                effective_overlay=effective_overlay,
                effective_manifest=effective_manifest,
                effective_compat=effective_compat,
                dashboard_assets=dashboard_assets,
                baseline_missing_files=baseline_missing_files,
                recovered_files=recovered_files,
            )
        except Exception as exc:
            # The user may already have had a partial state when this transaction
            # began; rollback restores *that exact state* rather than inventing a
            # new one. A later retry can still recover it from the pristine backup.
            rollback_errors: list[str] = []
            for plan in reversed(touched_seams):
                try:
                    if plan.get("existed_before", True):
                        backup_path = plan.get("txn_backup")
                        if backup_path is None:
                            raise OSError("transaction backup missing")
                        _atomic_copy(Path(backup_path), plan["dst"])
                    else:
                        plan["dst"].unlink(missing_ok=True)
                    _remove_pyc(plan["dst"])
                except OSError as rollback_exc:
                    rollback_errors.append(f"{plan['rel']}: {rollback_exc}")
            # Precommit failures must not undo unrelated writers' changes.
            # The runtime surfaces are ours only after runtime commit begins.
            if touched_runtime:
                for label, dst, snap, existed, directory in surface_snapshots:
                    try:
                        _restore_transaction_path(dst, snap, existed, directory=directory)
                    except OSError as rollback_exc:
                        rollback_errors.append(f"{label}: {rollback_exc}")

            suffix = (
                "; rollback issues: " + "; ".join(rollback_errors)
                if rollback_errors
                else ""
            )
            if isinstance(exc, IntegrationError):
                raise IntegrationError(str(exc) + suffix) from exc
            raise IntegrationError(f"integration transaction failed: {exc}{suffix}") from exc

    return {
        "ok": True,
        "restart_required": True,
        "crew_version": target.crew_version,
        "baseline": target.baseline,
        "legacy_home": str(old_home) if old_home else "",
        "app_home": str(app_home),
        "recovered_partial_files": recovered_files,
        "baseline_missing_files": sorted(set(baseline_missing_files)),
        "dashboard_assets": sorted(dashboard_assets),
    }


def _checked_uninstall_seam(crew_package: Path, rel: str) -> Path:
    """Resolve only literal Crew descendants; never trust recorded install JSON paths.

    Recorded paths can outlive upgrades and are read from mutable app data.
    Reject traversal, ambiguous separators, Windows drive syntax and links
    before either comparing or modifying a file.
    """
    if not rel.startswith(_PREFIX):
        raise IntegrationError(f"invalid recorded Crew seam path: {rel}")
    suffix = rel[len(_PREFIX):]
    parts = suffix.split("/")
    if any(part in ("", ".", "..") or ":" in part or "\x00" in part for part in parts):
        raise IntegrationError(f"invalid recorded Crew seam path: {rel}")
    live = crew_package.joinpath(*parts)
    if _crew_seam_is_symlink(crew_package, live):
        raise IntegrationError(f"recorded Crew seam is symlinked or outside Crew: {rel}")
    return live


def _recorded_uninstall_paths(state: dict[str, Any], key: str) -> list[str]:
    """Read an owned-file manifest without silently dropping malformed entries."""
    raw = state.get(key, [])
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise IntegrationError(f"invalid recorded {key} manifest; refusing uninstall")
    return [item.replace("\\", "/") for item in raw]


def _verify_uninstall_precommit(crew_package: Path, plans: list[dict[str, Any]]) -> None:
    """Recheck preflight identities immediately before the first destructive write.

    It is not a cross-process filesystem lock, but it prevents overwriting
    changes made while transaction snapshots were being constructed.
    """
    for plan in plans:
        live = _checked_uninstall_seam(crew_package, plan["rel"])
        if plan.get("missing"):
            if live.exists() or live.is_symlink():
                raise IntegrationError(f"Crew seam appeared during uninstall preflight: {plan['rel']}; retry")
        elif not live.is_file() or hashlib.sha256(live.read_bytes()).hexdigest() != plan["preflight_sha256"]:
            raise IntegrationError(f"Crew seam changed during uninstall preflight: {plan['rel']}; retry")


def uninstall_from_app(app_root: Path, app_home: Path) -> dict[str, Any]:
    """Restore pristine KiroCrew files and remove the Kiro BYO runtime.

    Uninstall is deliberately fail-closed.  Kiro BYO only restores/deletes a
    Crew seam when the live bytes still match the exact runtime snapshot from
    the last successful install.  If another extension or a Crew update changed
    a patched file afterwards, uninstall refuses rather than destroying those
    foreign changes.
    """
    state_path = app_home / "data" / "integration.json"
    state = _read_json(state_path)
    if not state:
        return {
            "ok": True,
            "installed": False,
            "restart_required": False,
            "restored_files": [],
            "removed_files": [],
            "note": "No recorded Kiro BYO integration was installed.",
        }

    crew_package = Path(str(state.get("crew_package") or "")).expanduser()
    site_packages = Path(str(state.get("site_packages") or "")).expanduser()
    crew_version = str(state.get("detected_version") or "").strip()
    backup_root = Path(str(state.get("backup_root") or "")).expanduser()
    runtime_overlay_root = Path(str(state.get("runtime_overlay_root") or "")).expanduser()
    if not crew_package.is_dir() or not site_packages.is_dir() or not crew_version:
        raise IntegrationError("recorded integration target is incomplete; repair/update Kiro BYO integration before uninstalling")
    if not backup_root.is_dir() or not runtime_overlay_root.is_dir():
        raise IntegrationError("pristine backup/runtime snapshot is missing; refusing destructive uninstall")

    generated = _recorded_uninstall_paths(state, "generated_files")
    dashboard_assets = _recorded_uninstall_paths(state, "dashboard_assets")
    created = _recorded_uninstall_paths(state, "created_files")
    # A seam must have exactly one cleanup action: conflicting/duplicate metadata
    # must not allow one path to be restored then deleted by another plan.
    all_seams = generated + dashboard_assets + created
    if len(set(all_seams)) != len(all_seams):
        raise IntegrationError("duplicate recorded Crew seam path; refusing ambiguous uninstall")
    plans: list[dict[str, Any]] = []
    for rel in generated + dashboard_assets:
        live = _checked_uninstall_seam(crew_package, rel)
        installed = runtime_overlay_root / rel
        pristine = backup_root / rel[len(_PREFIX):]
        if not live.is_file() or not installed.is_file() or not pristine.is_file():
            raise IntegrationError(f"cannot prove safe restore for {rel}; required live/snapshot/backup file is missing")
        try:
            if _normalized_sha(live) != _normalized_sha(installed):
                raise IntegrationError(
                    f"refusing to restore {rel}: Crew seam changed after Kiro BYO installed it"
                )
        except OSError as exc:
            raise IntegrationError(f"could not verify {rel}: {exc}") from exc
        plans.append({
            "kind": "restore", "rel": rel, "live": live, "pristine": pristine,
            "preflight_sha256": hashlib.sha256(live.read_bytes()).hexdigest(),
        })

    for rel in created:
        live = _checked_uninstall_seam(crew_package, rel)
        installed = runtime_overlay_root / rel
        if not live.exists():
            plans.append({"kind": "delete", "rel": rel, "live": live, "missing": True})
            continue
        if not live.is_file() or not installed.is_file():
            raise IntegrationError(f"cannot prove ownership of Kiro BYO-created seam {rel}")
        try:
            if _normalized_sha(live) != _normalized_sha(installed):
                raise IntegrationError(
                    f"refusing to delete {rel}: file no longer matches the Kiro BYO-installed snapshot"
                )
        except OSError as exc:
            raise IntegrationError(f"could not verify {rel}: {exc}") from exc
        plans.append({
            "kind": "delete", "rel": rel, "live": live, "missing": False,
            "preflight_sha256": hashlib.sha256(live.read_bytes()).hexdigest(),
        })

    runtime = site_packages / "kiro_byo"
    pth = site_packages / "kiro_byo_bootstrap.pth"
    if runtime.is_symlink() or pth.is_symlink():
        raise IntegrationError("installed runtime/bootstrap path is symlinked; refusing uninstall")
    if runtime.exists():
        if not runtime.is_dir():
            raise IntegrationError("site-packages/kiro_byo is not a directory; refusing to remove it")
        bootstrap_path = runtime / "_bootstrap_state.json"
        bootstrap = _read_json(bootstrap_path)
        if not bootstrap:
            raise IntegrationError("installed kiro_byo runtime has no ownership state; refusing to remove it")
        recorded_home = str(bootstrap.get("kiro_byo_home") or "")
        installed_via = str(bootstrap.get("installed_via") or "")
        if not recorded_home or Path(recorded_home).expanduser() != app_home:
            raise IntegrationError("installed kiro_byo runtime belongs to a different app data directory")
        if installed_via != "kirocrew-app":
            raise IntegrationError("installed kiro_byo runtime was not created by the KiroCrew App integration")
    if pth.exists():
        try:
            if pth.read_text(encoding="ascii") != "import kiro_byo.bootstrap_patch\n":
                raise IntegrationError("kiro_byo_bootstrap.pth contains foreign content; refusing to remove it")
        except UnicodeError as exc:
            raise IntegrationError("kiro_byo_bootstrap.pth contains foreign content; refusing to remove it") from exc

    txn_parent = app_home / "data" / "integration-transactions"
    txn_parent.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    removed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="kiro-byo-uninstall-", dir=str(txn_parent)) as td:
        txn = Path(td)
        snapshots: list[dict[str, Any]] = []
        for plan in plans:
            live = Path(plan["live"])
            snap = txn / "crew" / plan["rel"][len(_PREFIX):]
            snap.parent.mkdir(parents=True, exist_ok=True)
            snapshots.append({
                "live": live, "snap": snap,
                "existed": _snapshot_transaction_path(live, snap),
            })
        runtime_snap = txn / "runtime"
        runtime_existed = _snapshot_transaction_path(runtime, runtime_snap, directory=True)
        pth_snap = txn / "kiro_byo_bootstrap.pth"
        pth_existed = _snapshot_transaction_path(pth, pth_snap)
        state_snap = txn / "integration.json"
        _snapshot_transaction_path(state_path, state_snap)
        snapshots_by_live = {snap["live"]: snap for snap in snapshots}
        touched_seams: list[dict[str, Any]] = []
        touched_runtime = touched_pth = touched_state = False

        # A process/extension may have changed Crew after initial inspection.
        # Check *before* starting mutation, so a refusal never rolls back over
        # foreign changes or removes a third-party-owned newly created file.
        _verify_uninstall_precommit(crew_package, plans)
        try:
            for plan in plans:
                live = Path(plan["live"])
                if plan["kind"] == "restore":
                    touched_seams.append(snapshots_by_live[live])
                    _atomic_copy(Path(plan["pristine"]), live)
                    _remove_pyc(live)
                    restored.append(plan["rel"])
                elif not plan.get("missing"):
                    touched_seams.append(snapshots_by_live[live])
                    live.unlink(missing_ok=True)
                    _remove_pyc(live)
                    removed.append(plan["rel"])

            if runtime.is_dir():
                touched_runtime = True
                shutil.rmtree(runtime)
            touched_pth = True
            pth.unlink(missing_ok=True)
            touched_state = True
            state_path.unlink(missing_ok=True)

            # Once pristine Crew bytes are back, integration-only recovery data
            # is no longer needed. Provider config/telemetry are intentionally
            # retained unless the caller explicitly requests a full data purge.
            safe_version = _safe_version_token(crew_version)
            shutil.rmtree(app_home / "data" / "integration-runtime" / safe_version, ignore_errors=True)
            shutil.rmtree(backup_root, ignore_errors=True)
        except Exception as exc:
            rollback_errors: list[str] = []
            for snap in reversed(touched_seams):
                try:
                    _restore_transaction_path(snap["live"], snap["snap"], snap["existed"])
                    _remove_pyc(snap["live"])
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            for changed, label, dst, snap, existed, directory in (
                (touched_runtime, "runtime package", runtime, runtime_snap, runtime_existed, True),
                (touched_pth, "bootstrap pth", pth, pth_snap, pth_existed, False),
                (touched_state, "integration state", state_path, state_snap, True, False),
            ):
                if changed:
                    try:
                        _restore_transaction_path(dst, snap, existed, directory=directory)
                    except OSError as rollback_exc:
                        rollback_errors.append(f"{label}: {rollback_exc}")
            suffix = "; rollback issues: " + "; ".join(rollback_errors) if rollback_errors else ""
            if isinstance(exc, IntegrationError):
                raise IntegrationError(str(exc) + suffix) from exc
            raise IntegrationError(f"integration uninstall failed: {exc}{suffix}") from exc

    return {
        "ok": True,
        "installed": False,
        "restart_required": True,
        "crew_version": crew_version,
        "restored_files": restored,
        "removed_files": removed,
    }
