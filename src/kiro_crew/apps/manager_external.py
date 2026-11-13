"""Registering a self-managed (external) app, which installs and runs itself.

``register_external_app`` records the app's metadata so the dashboard can show it;
the app owns its own install and resource registration.

Part of the ``kiro_crew.apps.manager`` family: callers and tests reach every name here
as ``kiro_crew.apps.manager.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from kiro_crew.apps import protected_app_state
from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.execution import repository_bound_grant_denied
from kiro_crew.apps.host_exec_grants import grant_host_exec
from kiro_crew.apps.manager_builtins import _builtin_owns_install
from kiro_crew.apps.manager_records import (
    _AWAITING_MANIFEST_TEXT,
    APP_MANIFEST_FILENAME,
    INSTALLED_META_FILENAME,
    AppResult,
    InstalledApp,
    _approved_grants_after_manifest_change,
    _approved_grants_snapshot,
    _declared_grant_entries,
    _effective_approved_grants,
    _held_back_entries,
    _now_iso,
    _pending_session_approval_after_manifest_change,
    _read_installed,
    _restore_approved_grants,
    _session_control_widening,
    _write_approved_grants,
    _write_awaiting_manifest,
    _write_installed,
    app_data_dir,
    app_dir,
    get_app_manifest,
    hold_app_record_lock,
    holds_app_record_locks,
)
from kiro_crew.apps.manager_tree import _check_path_safety
from kiro_crew.apps.manifest import (
    RESERVED_APP_NAME_CODE,
    AppManifest,
    Permissions,
    app_name_error,
    cron_permission_error,
    is_reserved_app_name,
)
from kiro_crew.apps.point_grants import grant_source
from kiro_crew.atomic_write import atomic_write
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.manager")


# ---------------------------------------------------------------------------
# External (self-managed) app registration
# ---------------------------------------------------------------------------


@holds_app_record_locks
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
    source_repository: str = "",
    start_disabled: bool = False,
    version_checked: bool = False,
) -> AppResult:
    """Register a self-managed app with Kiro Crew's app system.

    ``version_checked=True`` says the caller already asked the governance
    ``app_versions`` list off the loop (``admission.app_version_refusal``), so
    admission skips that blocking read here.

    Self-managed apps (``resources="app"``) handle their own agent/skill/MCP
    registration.  Kiro Crew only tracks metadata so the dashboard can display them.

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
        source_repository: Server-resolved repository coordinate for a registry
            install/update. An empty value on an existing repository-owned record
            is a metadata refresh, not permission to erase its provenance.
        start_disabled: Create a new record disabled until an explicit enable.
            Existing records retain their enabled state, subject to consent.

    Returns:
        AppResult indicating success or failure.
    """
    if not _check_path_safety(name):
        return AppResult(ok=False, error=f"unsafe app name: {name!r}")

    # Enforce the canonical app-name contract on the self-registration path
    # (CWE-178). Admission normalizes with NFKC+casefold+strip, but the backend
    # below stores/resolves the app by the RAW name (app_dir(name),
    # _write_installed(name), write_app_secret(name)), so without this an
    # admitted "Safe-App"/"safe-app "/Unicode-equivalent would diverge from the
    # approved identity. install_app/update_app reach the same contract via
    # AppManifest.validate(); this closes the register_external gap.
    name_error = app_name_error(name)
    if name_error:
        return AppResult(
            ok=False,
            name=name,
            error=f"invalid app name: {name_error}",
            error_code=RESERVED_APP_NAME_CODE if is_reserved_app_name(name) else "",
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
            error="builtin origin is reserved for KiroCrew-shipped apps",  # brand-ok: BASE error text
        )

    # Admission: register_external_app writes enabled=True and is HTTP-reachable
    # (POST /api/apps/register), so it is an install+enable path and MUST be
    # gated too — otherwise a banned/non-allowlisted app can self-register and
    # activate with no admission control. Pass the self-reported manifest (when
    # provided) so a correctly-signed app is admitted under require_signature.
    admission_manifest = None
    # The manifest arrives from the request body. An empty value is no manifest,
    # as before; a non-empty one that is not a JSON object cannot be parsed.
    if manifest_data and not isinstance(manifest_data, dict):
        return AppResult(ok=False, name=name, error="manifest must be a JSON object")
    if manifest_data:
        admission_manifest = AppManifest.from_dict(manifest_data)
        # Self-registration parses without validating, and parsing records a
        # lifecycle timeout `int()` cannot read instead of raising. Refuse it here,
        # before any write, the way install validation does.
        setup_errors = admission_manifest.setup.validate()
        if setup_errors:
            return AppResult(ok=False, name=name, error="; ".join(setup_errors))
    denied = app_admission_denied(
        name,
        manifest=admission_manifest,
        action="register_external",
        check_version=not version_checked,
        version=version,
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

    hold_app_record_lock(name)
    existing = _read_installed(name)
    requested_repository = source_repository.strip()
    preserve_server_provenance = bool(
        existing and not requested_repository and existing.sourceUrl.strip()
    )
    # Self-managed registry apps use the public registration contract on every
    # launch. That request cannot carry a server-resolved clone coordinate, so an
    # omission refreshes app-owned metadata while the durable install coordinate
    # remains the authority for an existing grant. Only an internal caller that
    # supplies a non-empty repository can request a source transition.
    trust_repository = (
        existing.sourceUrl.strip()
        if preserve_server_provenance and existing is not None
        else requested_repository
    )
    trust_denied = repository_bound_grant_denied(name, repository=trust_repository)
    if trust_denied:
        sel().log_api_access(
            caller="app_register_external",
            operation="trust_repository",
            outcome="rejected",
            resources=f"name={name!r}",
            error=trust_denied,
        )
        return AppResult(
            ok=False,
            name=name,
            error=trust_denied,
            error_code="app_trust_repository_mismatch",
        )

    dest = app_dir(name)

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
                f"{name!r} is a KiroCrew-shipped builtin and cannot be replaced "  # brand-ok: BASE error text
                "by self-registration"
            ),
        )

    # Self-registration admits a new manifest the way install and update do, so it
    # applies the same cron-grant rule. A re-registration at the version already on
    # record, whose recorded manifest already scheduled crons without the grant, is
    # a routine relaunch of an app registered before the rule rather than an
    # update: it keeps registering so the app's crons keep firing, and the rule
    # reaches the app with its next version.
    if admission_manifest is not None:
        cron_grant_error = cron_permission_error(admission_manifest)
        if cron_grant_error and existing:
            recorded = get_app_manifest(name)
            if (
                recorded is not None
                and recorded.version == admission_manifest.version
                and cron_permission_error(recorded)
            ):
                cron_grant_error = ""
        if cron_grant_error:
            sel().log_api_access(
                caller="app_register_external",
                operation="manifest",
                outcome="rejected",
                resources=f"name={name!r}",
                error=cron_grant_error,
            )
            return AppResult(ok=False, name=name, error=f"invalid manifest: {cron_grant_error}")

    # Self-registration is routine (self-managed apps re-register on every
    # launch) and the app authors its own manifest, so this path can widen the
    # session-approval grant without any user moment -- the same gap
    # ``update_app`` closes with ``widened_session_approval``. Compare against the
    # manifest that was consented to (the persisted one; none for a first
    # registration) and, if the grant or its member-thread scope is new,
    # register the app DISABLED so the user sees it on the detail page and
    # enables it deliberately.
    requested_permissions = Permissions.from_dict(
        manifest_data["permissions"]
        if isinstance(manifest_data, dict) and isinstance(manifest_data.get("permissions"), dict)
        else {}
    )
    requested_session_approval = requested_permissions.sessionApproval
    prior_manifest = get_app_manifest(name) if existing else None
    session_control_widening = _session_control_widening(
        prior_manifest.permissions if prior_manifest else None, requested_permissions
    )
    widened_session_approval = session_control_widening is not None
    # Same holding back as ``update_app`` for added api/events entries. A first
    # registration approves what it declares: there is no earlier approval to
    # widen, as with an install.
    new_permissions = grant_source(manifest_data) if isinstance(manifest_data, dict) else None
    prior_grants = _effective_approved_grants(name, existing) if existing else None
    # The stored set as it stands, for a rollback to put back exactly.
    try:
        prior_grants_text = _approved_grants_snapshot(name) if existing else None
    except (OSError, ValueError) as exc:
        # Before any write: nothing to put back yet.
        return AppResult(
            ok=False, name=name, error=f"could not read the approved permissions of {name!r}: {exc}"
        )
    prior_owner_disabled = protected_app_state.owner_disabled(name)
    approved_grants: dict[str, list[str]] | None
    if not manifest_data:
        if existing:
            approved_grants = _effective_approved_grants(name, existing)
        elif (stored_text := _approved_grants_snapshot(name)) is not None:
            # A record the folder lost (renamed aside, recreated): the sealed set
            # stands, a registration still awaiting its first manifest included. The
            # folder's own app.json, which an agent can write, never decides it.
            approved_grants = (
                None
                if stored_text == _AWAITING_MANIFEST_TEXT
                else _effective_approved_grants(name, None)
            )
        else:
            approved_grants = None
    elif existing and prior_grants_text == _AWAITING_MANIFEST_TEXT:
        # The first manifest of a registration that declared nothing, sent by this
        # owner route: approved as declared, as a first registration with it would be.
        # The sealed store, not the agent-writable app.json, says none came before.
        approved_grants = _declared_grant_entries(new_permissions)
    elif existing:
        approved_grants = _approved_grants_after_manifest_change(
            existing_approved=prior_grants,
            old_permissions=grant_source(prior_manifest),
            new_permissions=new_permissions,
        )
    else:
        approved_grants = _declared_grant_entries(new_permissions)
    widened_grants = bool(manifest_data) and any(
        _held_back_entries(approved_grants, new_permissions).values()
    )

    if existing:
        # Build replacement metadata without mutating the persisted snapshot;
        # it remains the rollback source if either durable write fails.
        meta = replace(
            existing,
            version=version,
            displayName=display_name,
            updatedAt=_now_iso(),
            enabled=False if widened_session_approval else existing.enabled,
            sessionApprovalConsentPending=(
                _pending_session_approval_after_manifest_change(
                    existing_pending=existing.sessionApprovalConsentPending,
                    requested_session_approval=requested_session_approval,
                    widened_session_approval=widened_session_approval,
                )
                if manifest_data
                else existing.sessionApprovalConsentPending
            ),
            resources=resources,
            lifecycle=lifecycle,
            approvedGrantsStored=existing.approvedGrantsStored or bool(manifest_data),
        )
        if not preserve_server_provenance:
            if source:
                meta.source = source
            meta.sourceUrl = requested_repository
            meta.sourceRegistry = ""
            meta.sourceCommit = ""
            meta.sourceSigner = ""
            meta.origin = origin

        manifest_path = dest / APP_MANIFEST_FILENAME
        prior_manifest_text = (
            manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else None
        )
        # The record as it is on disk, restored byte for byte on rollback. A
        # rewrite of ``existing`` would add a consent key that a record from
        # before the key existed never had, and could record no consent needed.
        meta_path = dest / INSTALLED_META_FILENAME
        prior_meta_text = meta_path.read_text(encoding="utf-8") if meta_path.is_file() else None
        manifest_text = json.dumps(manifest_data, indent=2) + "\n" if manifest_data else ""
        try:
            if manifest_data:
                # Narrowing first: the new set is a subset of the old one, so
                # it is safe beside either manifest.
                _write_approved_grants(name, approved_grants)
            if manifest_data and (widened_session_approval or widened_grants):
                # Disable (or hold back) first when adding a grant so the new
                # manifest is never live beside metadata that authorizes it.
                _write_installed(name, meta)
                atomic_write(manifest_path, manifest_text)
            else:
                # Remove the grant durably before clearing pending consent.
                if manifest_data:
                    atomic_write(manifest_path, manifest_text)
                _write_installed(name, meta)
        except (OSError, ValueError) as exc:
            rollback_errors: list[str] = []
            try:
                # The set first, exactly as it was before this registration.
                if manifest_data:
                    _restore_approved_grants(name, prior_grants_text)
                # A widening write that turned the app off recorded a disable; the
                # rollback puts the record back, so it puts that mark back too.
                protected_app_state.set_owner_disabled(name, prior_owner_disabled)
                if prior_meta_text is not None:
                    atomic_write(meta_path, prior_meta_text)
                else:
                    _write_installed(name, existing)
            except OSError as rollback_exc:
                rollback_errors.append(f"metadata rollback failed: {rollback_exc}")
            try:
                if manifest_data:
                    if prior_manifest_text is None:
                        manifest_path.unlink(missing_ok=True)
                    else:
                        atomic_write(manifest_path, prior_manifest_text)
            except OSError as rollback_exc:
                rollback_errors.append(f"manifest rollback failed: {rollback_exc}")
            detail = f"failed to persist external registration: {exc}"
            if rollback_errors:
                detail += f" ({'; '.join(rollback_errors)})"
            return AppResult(ok=False, name=name, error=detail)
    else:
        # New registration
        dest.mkdir(parents=True, exist_ok=True)
        meta = InstalledApp(
            name=name,
            version=version,
            displayName=display_name,
            # Callers that guard activation separately need the first durable
            # record disabled, just as a new session-control grant needs consent.
            enabled=not (widened_session_approval or start_disabled),
            sessionApprovalConsentPending=widened_session_approval,
            installedAt=_now_iso(),
            source=source,
            sourceUrl=requested_repository,
            origin=origin,
            resources=resources,
            lifecycle=lifecycle,
            approvedGrantsStored=approved_grants is not None,
        )
        if approved_grants is not None:
            _write_approved_grants(name, approved_grants)
        else:
            # Nothing declared yet: the first manifest this route sends is approved
            # as declared; one written into the app's folder approves nothing.
            _write_awaiting_manifest(name)
        _write_installed(name, meta)
        # Persist manifest if provided (so dashboard can show full info).
        if manifest_data:
            manifest_path = dest / APP_MANIFEST_FILENAME
            atomic_write(manifest_path, json.dumps(manifest_data, indent=2) + "\n")

    # Ensure data directory exists
    app_data_dir(name)

    # Generate app secret only for new registrations — preserve existing secrets
    from kiro_crew.dashboard.token_auth import generate_app_secret, write_app_secret

    # The stored record alone: a registration from before the gateway kept it gets a new
    # secret IN ITS RESPONSE, the only channel a self-managed app has, rather than one
    # replaced behind its back.
    try:
        has_secret = protected_app_state.read_secret(name) is not None
    except (OSError, UnicodeDecodeError, ValueError):
        has_secret = False
    is_new_secret = not (existing and has_secret)
    if is_new_secret:
        secret = generate_app_secret()
        write_app_secret(name, secret)
        if not existing and meta.enabled:
            # A first registration is an owner-authenticated install+enable (the
            # route requires an owner dashboard request), so it records the owner-
            # enable marker the token exchange reads (``validate_app_secret``). A
            # re-registration never does: it would read ``enabled`` back from the
            # agent-writable installed.json.
            if not grant_host_exec(name):
                # The record is written; enabling it (the "already enabled" path)
                # retries the marker, which a re-registration would not.
                return AppResult(
                    ok=False,
                    name=name,
                    error=(
                        f"registered {name}, but could not record that the owner "
                        "enabled it; free up space or fix the data folder's "
                        "permissions, then enable the app in the App Store"
                    ),
                )
    else:
        # Every caller is owner-authenticated, and the folder holds no copy, so the
        # response is how a self-managed app gets its secret back, e.g. after a
        # registry install registered it first.
        try:
            secret = protected_app_state.read_secret(name) or ""
        except (OSError, UnicodeDecodeError, ValueError):
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
    if session_control_widening is not None:
        sel().log_api_access(
            caller="app_register",
            operation="session_approval_widened",
            outcome="disabled",
            resources=f"name={name!r}",
            error=f"registration added {session_control_widening.keys}; re-enable to consent",
        )
        return AppResult(
            ok=True,
            name=name,
            message=(
                f"{action} {name} v{version}; disabled because this manifest newly "
                f"requests {session_control_widening.words} -- review it on the app page "
                "and enable it"
            ),
            secret=secret,
            notice="session_approval_reconsent",
        )
    if widened_grants:
        sel().log_api_access(
            caller="app_register",
            operation="grants_widened",
            outcome="staged",
            resources=f"name={name!r}",
            error="registration added permissions.api/events entries; approve to grant them",
        )
    result = AppResult(
        ok=True,
        name=name,
        message=f"{action} {name} v{version}",
        secret=secret,
    )
    return result
