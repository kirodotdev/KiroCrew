"""Central execution boundary for App Kit apps.

App admission and governance decide which apps may be installed or activated.
This module answers the separate runtime question: whether executable code from
an admitted app may run in the gateway's trust domain.  Keeping that decision in
one place prevents Python hooks, backend processes, and manifest shell commands
from drifting to different defaults.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_BUILTINS_DIR = (Path(__file__).resolve().parent / "builtins").resolve()
_CONFIG_KEY = "agent.apps_allow_third_party"


def _builtin_manifest_sources() -> tuple[Path, ...]:
    """Return resolved builtin-manifest roots from core and the active edition.

    The platform seam is imported lazily because ``apps.manager`` imports this
    module while the platform graph is still being composed. A missing or
    unreadable edition source is omitted (fail-closed for that source); it can
    never widen provenance to a mutable installed-app directory.
    """
    sources: list[Path] = [_BUILTINS_DIR]
    try:
        from kiro_crew.platform import current_context

        sources.extend(
            Path(source)
            for source in current_context().apps_loader.manifest_sources()
        )
    except Exception:  # noqa: BLE001 - unavailable composition must not admit code
        logger.debug("edition builtin manifest sources unavailable", exc_info=True)

    roots: list[Path] = []
    seen: set[Path] = set()
    for source in sources:
        try:
            root = source.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if not root.is_dir() or root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return tuple(roots)


def shipped_builtin_app_root(app_name: str) -> Path | None:
    """Return the immutable package directory that ships ``app_name``.

    The package ``app.json`` is authoritative. Mutable installed metadata is
    deliberately ignored, and a directory without a valid shipped manifest is
    not a builtin even when its name resembles one.
    """
    for source in _builtin_manifest_sources():
        try:
            entries = sorted(source.iterdir())
        except OSError:
            continue

        for entry in entries:
            try:
                root = entry.resolve(strict=True)
                if not root.is_dir() or not root.is_relative_to(source):
                    continue
                manifest_path = (root / "app.json").resolve(strict=True)
                if not manifest_path.is_file() or not manifest_path.is_relative_to(root):
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict) and manifest.get("name") == app_name:
                return root
    return None


def shipped_builtin_module_path(app_name: str, module_name: str) -> Path | None:
    """Resolve a ``python -m`` target only when it belongs to ``app_name``'s package."""
    root = shipped_builtin_app_root(app_name)
    if root is None:
        return None
    module_parts = module_name.split(".")
    if not module_parts or not all(part.isidentifier() for part in module_parts):
        return None

    # Reconstruct the dotted module from each ancestor of the immutable app
    # root. This supports both core modules (``kiro_crew.apps.builtins.*``)
    # and edition package namespaces without importing an attacker-selected
    # parent package merely to call ``find_spec``.
    for base in root.parents:
        target = base.joinpath(*module_parts)
        for candidate in (target.with_suffix(".py"), target / "__main__.py"):
            try:
                resolved = candidate.resolve(strict=True)
                if resolved.is_file() and resolved.is_relative_to(root):
                    return resolved
            except (OSError, ValueError):
                continue
    return None


def is_builtin_app(
    *, app_root: Path | None = None, app_name: str | None = None
) -> bool:
    """Return whether immutable package provenance covers the executed path.

    Path-only callers must point inside one of the composed shipped-manifest
    roots. When an app name is available, its shipped manifest must declare
    that exact name and the executable path must resolve inside that app's
    package directory. Mutable ``installed.json`` fields are never provenance.
    """
    if app_root is None:
        return False
    try:
        resolved = app_root.resolve(strict=True)
    except (OSError, ValueError):
        return False
    if app_name is None:
        return any(
            resolved.is_relative_to(source)
            for source in _builtin_manifest_sources()
        )
    shipped_root = shipped_builtin_app_root(app_name)
    return shipped_root is not None and resolved.is_relative_to(shipped_root)


def third_party_execution_allowed() -> bool:
    """Return the operator's explicit third-party execution decision.

    Absence, malformed values, and config-load failures all deny.  The strict
    identity check intentionally rejects truthy values such as ``1`` or
    ``"true"``; only a validated JSON boolean ``true`` is an admission.
    Environment variables are not consulted, so a child/app-controlled env value
    cannot widen this process-level trust boundary.
    """
    try:
        # Deferred to avoid importing the full config graph while it imports apps.
        from kiro_crew.config.loader import KiroCrewConfig

        value = getattr(KiroCrewConfig.load().agent, "apps_allow_third_party", False)
        return value is True
    except Exception as exc:  # noqa: BLE001 - unreadable policy must fail closed
        logger.error(
            "%s: config load failed (%s); refusing third-party app execution",
            _CONFIG_KEY,
            exc,
        )
        return False


def app_execution_denied(
    app_name: str,
    *,
    action: str,
    app_root: Path | None = None,
    caller: str = "gateway",
) -> str | None:
    """Return a denial reason when an app execution surface must not run.

    Shipped package code is exempt only when ``app_root`` resolves inside the
    immutable builtin package registered for ``app_name``.  Every other target
    requires ``agent.apps_allow_third_party`` to be the JSON boolean ``true``.
    Allowed and denied decisions are audited best-effort, but audit
    unavailability never changes the execution decision.
    """
    builtin = is_builtin_app(app_name=app_name, app_root=app_root)
    provenance = (
        "provenance=shipped_builtin" if builtin else "provenance=unverified"
    )
    if builtin or third_party_execution_allowed():
        try:
            sel().log_api_access(
                caller=caller,
                operation="app_execution_admission",
                outcome="allowed",
                resources=f"app={app_name} action={action} {provenance}",
            )
        except Exception:  # noqa: BLE001 - admission must survive audit unavailability
            logger.debug("app execution admission audit failed", exc_info=True)
        return None

    reason = (
        "third-party app execution is disabled; explicitly set "
        f"{_CONFIG_KEY}=true to allow Python, backend, and manifest shell code"
    )
    try:
        sel().log_api_access(
            caller=caller,
            operation="app_execution_admission",
            outcome="denied",
            resources=f"app={app_name} action={action} {provenance}",
            error=reason,
        )
    except Exception:  # noqa: BLE001 - denial must survive audit unavailability
        logger.debug("app execution denial audit failed", exc_info=True)
    return reason
