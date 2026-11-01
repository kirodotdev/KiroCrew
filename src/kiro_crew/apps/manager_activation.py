"""Switching an installed app on and off.

``enable_app``, ``disable_app`` and the governance check every activation passes
(``_app_activation_denied``).

Part of the ``kiro_crew.apps.manager`` family: callers and tests reach every name here
as ``kiro_crew.apps.manager.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import json
import logging

from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.execution import app_execution_denied, shipped_builtin_app_root
from kiro_crew.apps.manager_records import (
    INSTALLED_META_FILENAME,
    SESSION_APPROVAL_PENDING_KEY,
    STAGED_GRANT_FAMILIES,
    AppResult,
    _declared_grant_entries,
    _effective_approved_grants,
    _now_iso,
    _read_installed,
    _session_approval_consent_unrecorded,
    _write_approved_grants,
    _write_installed,
    app_dir,
    apps_dir,
    get_app_manifest,
    hold_app_record_lock,
    holds_app_record_locks,
)
from kiro_crew.apps.manager_tree import _check_path_safety
from kiro_crew.apps.point_grants import grant_source
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.manager")


# ---------------------------------------------------------------------------
# Enable / Disable
# ---------------------------------------------------------------------------


def _app_activation_denied(name: str, *, fail_closed: bool = False) -> str | None:
    """Return a denial reason if governance forbids activating app *name*, else None.

    The ``apps`` scope (a ScopedRuleset over app slugs) is the per-app activation
    allowlist: an enterprise policy may restrict which apps may run at all (e.g.
    ``apps: {mode: allow, allow: ["auto-research", "file-explorer"]}``).  Enabling
    is the activation chokepoint — a disabled app contributes no agents, skills,
    crons, or routes — so the gate lives here.  Resolution uses the ``_host``
    session key (surface ``host``): app activation is an operator/host action, so
    it is governed by the policy ceiling AND any ``bind: {type: surface, id:
    host}`` profile — an honest, stable bind target.  (It must NOT use an empty
    key, which classifies to surface ``unknown`` and silently matches nothing.)
    By default, a ``PlatformCompositionError`` propagates while any other
    evaluation error degrades to "no opinion".  With ``fail_closed=True``, the
    evaluator receives the strict disposition and any escaped evaluation error
    becomes a denial reason.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import (
            HOST_SESSION_KEY,
            governance_permits,
        )

        decision = governance_permits(
            "apps", name, session_key=HOST_SESSION_KEY, fail_closed=fail_closed
        )
        if not getattr(decision, "permitted", True):
            try:
                from kiro_crew.sel import sel

                sel().log_governance_decision(
                    session_key=HOST_SESSION_KEY,
                    tool_name=f"enable_app:{name}",
                    scope="apps",
                    item=name,
                    outcome="denied",
                    rule=getattr(decision, "rule", ""),
                    layer=getattr(decision, "layer", ""),
                    reason=getattr(decision, "reason", ""),
                )
            except Exception:
                logger.debug("app activation deny audit failed", exc_info=True)
            return getattr(decision, "reason", f"app {name!r} not permitted by policy")
        return None
    except PlatformCompositionError:
        raise
    except Exception as exc:
        # scope="apps" + app=name so the SEL records WHICH app's activation gate
        # degraded; session_key=_host so the SEL source is the honest "host"
        # surface (not "unknown"/"slack").  Wrapped so a late-import failure cannot
        # escape this branch and change its configured disposition.
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
        if fail_closed:
            return f"governance evaluation error: {exc}"
        return None


@holds_app_record_locks
def enable_app(
    name: str,
    *,
    session_approval_consent: bool = False,
    grants_consent: dict[str, list[str]] | None = None,
) -> AppResult:
    """Enable an installed app.

    ``grants_consent`` names the held-back ``permissions.api``/``events``
    entries the owner was shown and approved (see ``approvedGrants``). Only
    those, and only while the live manifest still declares them, join the
    approved set; an entry added after the owner looked stays held back.
    Without it the app is enabled on the approved set.
    """
    if not _check_path_safety(name):
        return AppResult(ok=False, name=name, error=f"unsafe app name: {name!r}")
    hold_app_record_lock(name)
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
            return AppResult(ok=False, name=name, error=f"blocked by admission policy: {denied}")

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

    if meta.sessionApprovalConsentPending and not session_approval_consent:
        return AppResult(
            ok=False,
            name=name,
            error="session approval consent must be confirmed from a disclosure surface",
            error_code="session_approval_consent_required",
        )

    current_grants = _effective_approved_grants(name, meta) if grants_consent is not None else None
    approve_grants = grants_consent is not None and current_grants is not None
    # A pending record still needs the consent written, even when it reads
    # enabled (one from before the consent marker existed).
    if meta.enabled and not approve_grants and not meta.sessionApprovalConsentPending:
        return AppResult(ok=True, name=name, message=f"{name} is already enabled")

    still_staged = False
    if approve_grants and grants_consent is not None and current_grants is not None:
        live = get_app_manifest(name)
        if live is None:
            # Nothing declared can be approved; say so rather than report an
            # approval that changed nothing.
            return AppResult(
                ok=False,
                name=name,
                error="the app manifest cannot be read right now; try approving again",
                error_code="grants_manifest_unreadable",
            )
        declared = _declared_grant_entries(grant_source(live))
        approved: dict[str, list[str]] = {}
        for family in STAGED_GRANT_FAMILIES:
            shown = {e for e in grants_consent.get(family, ()) if isinstance(e, str)}
            # A family the stored set predates approves what is declared.
            kept = list(current_grants[family] if family in current_grants else declared[family])
            kept += [e for e in declared[family] if e in shown and e not in kept]
            approved[family] = kept
            still_staged = still_staged or any(e not in kept for e in declared[family])
        _write_approved_grants(name, approved)
        meta.approvedGrantsStored = True
        sel().log_api_access(
            caller="app_enable",
            operation="grants_approved",
            outcome="partial" if still_staged else "success",
            resources=f"name={name!r}",
        )
    meta.enabled = True
    meta.sessionApprovalConsentPending = False
    meta.updatedAt = _now_iso()
    _write_installed(name, meta, session_approval_consent=session_approval_consent)

    logger.info("Enabled app %s", name)
    return AppResult(
        ok=True,
        name=name,
        message=f"enabled {name}",
    )


def _session_approval_consent_unrecorded_on_disk(name: str) -> bool:
    """Whether *name*'s record on disk still owes a ``sessionApproval`` consent step.

    Either the record has no consent key while its manifest needs one, or it
    reads both enabled and pending. Every path that sets pending also disables
    the app, so the second shape comes only from a key-less record that another
    writer read (``_read_installed`` marks it pending) and saved before this
    reconcile ran.
    """
    try:
        raw = json.loads((app_dir(name) / INSTALLED_META_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if (
        isinstance(raw, dict)
        and raw.get("enabled", True) is not False
        and raw.get(SESSION_APPROVAL_PENDING_KEY)
    ):
        manifest = get_app_manifest(name)
        return bool(manifest and manifest.permissions.sessionApproval)
    return _session_approval_consent_unrecorded(name, raw)


def session_approval_consent_candidates() -> list[str]:
    """Installed apps whose ``sessionApproval`` grant was never consented to.

    A host upgrade changes the parser, not the manifest: an app installed when
    ``permissions.sessionApproval`` was still ignored may already declare it,
    and its record has no consent marker. Read-only; the caller marks each one
    with :func:`mark_session_approval_consent_pending` under the app's
    lifecycle lock.
    """
    try:
        entries = sorted(apps_dir().iterdir())
    except OSError:
        return []
    return [
        entry.name
        for entry in entries
        if not entry.name.startswith(".")
        and _check_path_safety(entry.name)
        and _session_approval_consent_unrecorded_on_disk(entry.name)
    ]


@holds_app_record_locks
def mark_session_approval_consent_pending(name: str) -> bool:
    """Disable *name* and mark its ``sessionApproval`` grant pending, then audit it.

    Re-checks the record first, so a record uninstalled, consented or rewritten
    since :func:`session_approval_consent_candidates` is left alone. The caller
    holds ``app_lifecycle_lock(name)``, so no install, update or uninstall runs
    between this check and the write. The write adds the marker, so each record
    is handled once. Re-enabling goes through the disclosure surface.
    """
    hold_app_record_lock(name)
    if not _session_approval_consent_unrecorded_on_disk(name):
        return False
    meta = _read_installed(name)
    if meta is None:
        return False
    meta.enabled = False
    meta.sessionApprovalConsentPending = True
    meta.updatedAt = _now_iso()
    _write_installed(name, meta)
    try:
        sel().log_api_access(
            caller="app_boot",
            operation="session_approval_widened",
            outcome="disabled",
            resources=f"name={name!r}",
            error="install predates the consent record for permissions.sessionApproval; "
            "re-enable to consent",
        )
    except Exception:  # noqa: BLE001 - the disable already happened
        logger.warning("could not audit session approval reconcile for %r", name, exc_info=True)
    return True


@holds_app_record_locks
def disable_app(name: str) -> AppResult:
    """Disable an installed app without removing it."""
    if not _check_path_safety(name):
        return AppResult(ok=False, name=name, error=f"unsafe app name: {name!r}")
    hold_app_record_lock(name)
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
