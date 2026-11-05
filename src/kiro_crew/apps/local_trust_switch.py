"""Move a local-only execution grant to the repository a registry update installs.

An app installed from a local folder holds a LOCAL grant in
``agent.apps_trusted``. New grants also carry an ``agent.apps_trusted_local``
marker; legacy grants do not. Neither shape covers repository code, so a registry
update of the app is refused with ``app_trust_local_only``, and the consent dialog
cannot help: the grant endpoint binds to the INSTALLED source, which is the folder.

``POST /api/apps/{name}/update`` with ``source: "registry:<name>"`` and the
refusal's ``trustRepository`` as consent proof performs the switch instead. This
module holds its three steps, which the route runs under the app's lifecycle lock
through :func:`switch_to_repository` (steps 1 and 2) and :func:`revert_switch`
(step 3), each audited under the grant route's SEL operation:

1. :func:`check_switch` verifies the proof against the row
   ``install_from_registry`` will act on, and that the grant is a name-only grant
   over positively local installed code. It writes nothing.
2. :func:`bind_repository` records the repository-bound grant the consent dialog
   records for a fresh install, removing the local marker when one exists, before
   any repository bytes are fetched. It checks the grant again inside the locked
   write and reports whether the marker existed.
3. :func:`restore_local_grant` puts the name-only grant back in its original
   marker shape when the update fails while the installed provenance remains
   positively local. If repository provenance is committed, it leaves the repository
   binding in place for the installed code.

The grant shapes match ``security_trusted_apps.api_trusted_app_grant`` and
``default_installs._grant`` / ``_revoke``, and every write goes through the one
trust writer (``agent_config_writer.mutate_trust_settings``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Refused when the proof does not match the row the update would install.
MISMATCH_CODE = "app_trust_repository_mismatch"
#: Refused when the app's grant is not the local-only grant this switch replaces.
NOT_APPLICABLE_CODE = "app_trust_switch_not_applicable"
_NOT_APPLICABLE_ERROR = (
    "this app's trust is not the local-only grant that trusting its repository "
    "replaces; refresh the app page and update it again"
)


@dataclass(frozen=True)
class SwitchRefusal:
    """Why the switch cannot run. Nothing was written."""

    status: int
    code: str
    error: str


@dataclass(frozen=True)
class SwitchedGrant:
    """The repository binding written and the local-marker shape it replaced."""

    repository: str
    had_local_marker: bool


def _grant_is_local_only(name: str) -> bool:
    """Whether *name* has an unbound grant over positively local installed code."""
    from kiro_crew.apps.execution import (
        builtin_app_names,
        trusted_app_names,
        trusted_app_repository,
    )
    from kiro_crew.apps.registry import _trust_grant_would_stay_local

    if name in builtin_app_names():
        return False
    if name not in trusted_app_names():
        return False
    if trusted_app_repository(name):
        return False
    return _trust_grant_would_stay_local(name)


def _install_repository(name: str) -> str:
    """The normalized clone target ``install_from_registry(name)`` resolves, or ``""``."""
    from kiro_crew.apps import registry as reg

    entry, refusal = reg._resolve_install_entry(name)
    if refusal or not entry:
        return ""
    url = reg._entry_git_url(entry)
    if not url or reg._git_target_is_unsupported(url):
        return ""
    return reg._normalize_git_target(url)


def _check_switch_sync(name: str, consent: str) -> str | SwitchRefusal:
    from kiro_crew.apps import registry as reg

    if reg._git_target_is_unsupported(consent):
        return SwitchRefusal(
            400,
            "invalid_repository_consent",
            "repository consent proof contains an unsupported query or fragment "
            "or an ambiguous Git transport identity",
        )
    if not _grant_is_local_only(name):
        return SwitchRefusal(409, NOT_APPLICABLE_CODE, _NOT_APPLICABLE_ERROR)
    repository = _install_repository(name)
    # The proof binds the click to what the dialog showed. The registry row can
    # change between the refusal and the click; a stale proof is refused the way
    # the grant endpoint refuses one, and nothing is written. The coordinates are
    # comparison inputs only, never response or audit text.
    if not repository or repository != reg._normalize_git_target(consent):
        return SwitchRefusal(
            409,
            MISMATCH_CODE,
            "app repository changed since the consent dialog was opened; refresh and review it",
        )
    return repository


async def check_switch(name: str, consent: str) -> str | SwitchRefusal:
    """Return the repository to bind *name* to, or why the switch is refused.

    Blocking reads (config, installed metadata, the catalog) run off the loop.
    """
    return await asyncio.to_thread(_check_switch_sync, name, consent)


class GrantChanged(Exception):
    """The local-only grant was gone by the time the config lock was held."""


async def bind_repository(name: str, repository: str) -> bool:
    """Bind *name* to *repository* and report whether it had a local marker.

    The grant is checked again inside the locked write, against the file itself:
    :func:`check_switch` read it before the lock, and another process (the CLI,
    an uninstall) can revoke or rebind it in between. Moving a grant that is no
    longer there would put back a grant the operator removed, so that raises
    :class:`GrantChanged` and writes nothing. Positively local installed
    provenance is rechecked there too. Also raises the trust writer's
    ``ConfigCorruptError`` / ``TrustSettingOverlayOwned``.
    """
    from kiro_crew.apps.agent_config_writer import mutate_trust_settings
    from kiro_crew.apps.registry import _trust_grant_would_stay_local
    from kiro_crew.dashboard.handlers.security_trusted_apps import (
        _trusted_list_raw,
        _trusted_local_list_raw,
        _trusted_repositories_raw,
    )

    had_local_marker = False

    def _mutate(agent_raw: dict) -> None:
        nonlocal had_local_marker
        local = _trusted_local_list_raw(agent_raw)
        raw_bindings = agent_raw.get("apps_trusted_repositories")
        bound = isinstance(raw_bindings, dict) and bool(raw_bindings.get(name))
        if (
            name not in _trusted_list_raw(agent_raw)
            or bound
            or not _trust_grant_would_stay_local(name)
        ):
            raise GrantChanged(name)
        had_local_marker = name in local
        repositories = _trusted_repositories_raw(agent_raw)
        repositories[name] = repository
        agent_raw["apps_trusted_repositories"] = repositories
        if had_local_marker:
            agent_raw["apps_trusted_local"] = [a for a in local if a != name]

    await mutate_trust_settings(_mutate)
    return had_local_marker


async def restore_local_grant(name: str, switched: SwitchedGrant) -> bool:
    """Undo :func:`bind_repository` while installed provenance stays local.

    Only a binding that still names ``switched.repository`` over positively local
    installed code is undone: a grant the operator changed while the update ran is
    theirs and is left as it is, and repository provenance keeps the matching
    repository binding. The name stays in ``apps_trusted`` (it was there before the
    switch), and the local marker is restored only when the original grant had one.
    A failed write is logged and leaves the repository grant, which does not cover
    restored local code: the app is then refused, never widened.
    """
    from kiro_crew.apps.agent_config_writer import mutate_trust_settings
    from kiro_crew.apps.registry import _trust_grant_would_stay_local
    from kiro_crew.dashboard.handlers.security_trusted_apps import (
        _trusted_local_list_raw,
        _trusted_repositories_raw,
    )

    restored = False

    def _mutate(agent_raw: dict) -> None:
        nonlocal restored
        repositories = _trusted_repositories_raw(agent_raw)
        if repositories.get(name) != switched.repository or not _trust_grant_would_stay_local(name):
            return
        repositories.pop(name, None)
        agent_raw["apps_trusted_repositories"] = repositories
        local = _trusted_local_list_raw(agent_raw)
        if switched.had_local_marker:
            if name not in local:
                local.append(name)
            agent_raw["apps_trusted_local"] = local
        elif name in local:
            agent_raw["apps_trusted_local"] = [app for app in local if app != name]
        restored = True

    try:
        await mutate_trust_settings(_mutate)
    except Exception:  # noqa: BLE001 - the caller must still restore the old app
        # Any failure, a full disk included: the route restores the old app
        # after this returns, and an exception here would skip that and leave
        # it stopped. The repository grant that stays fails closed.
        logger.warning(
            "could not restore the local grant after a failed update of %r", name, exc_info=True
        )
        return False
    return restored


#: Every grant write is audited under the trusted-apps grant route's operation.
_GRANT_OPERATION = "security.trusted_apps.grant"


def _audit(caller: str, outcome: str, resources: str) -> None:
    """Best-effort SEL row; losing it never changes the outcome."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=_GRANT_OPERATION,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:  # noqa: BLE001 - an audit failure must not mask the result
        logger.warning("SEL logging failed for %s", _GRANT_OPERATION, exc_info=True)


async def switch_to_repository(
    name: str, consent: str, *, caller: str
) -> SwitchedGrant | SwitchRefusal:
    """Check consent and bind *name* to the repository. Steps 1 and 2.

    Returns the binding and original marker state, or the refusal to send; a
    refusal wrote nothing. The caller holds ``app_lifecycle_lock(name)``.
    """
    from kiro_crew.dashboard.handlers.security_audit import ConfigCorruptError
    from kiro_crew.dashboard.handlers.security_trusted_apps import TrustSettingOverlayOwned

    checked = await check_switch(name, consent)
    if isinstance(checked, SwitchRefusal):
        _audit(caller, "denied", f"{name}={checked.code}")
        return checked
    try:
        had_local_marker = await bind_repository(name, checked)
    except GrantChanged:
        _audit(caller, "denied", f"{name}={NOT_APPLICABLE_CODE}")
        return SwitchRefusal(409, NOT_APPLICABLE_CODE, _NOT_APPLICABLE_ERROR)
    except ConfigCorruptError as exc:
        _audit(caller, "denied", f"{name}=config_corrupt")
        return SwitchRefusal(409, "config_corrupt", str(exc))
    except TrustSettingOverlayOwned as exc:
        _audit(caller, "denied", f"{name}=overlay_owned")
        return SwitchRefusal(409, "trust_setting_overlay_owned", str(exc))
    _audit(caller, "ok", f"{name}=registry_update")
    return SwitchedGrant(checked, had_local_marker)


async def revert_switch(name: str, switched: SwitchedGrant, *, caller: str) -> None:
    """Step 3 after a failed update: restore the prior grant shape and audit it."""
    restored = await restore_local_grant(name, switched)
    _audit(caller, "reverted" if restored else "revert_skipped", f"{name}=registry_update")


async def switch_cancel_safe(
    name: str, consent: str, *, caller: str
) -> SwitchedGrant | SwitchRefusal:
    """:func:`switch_to_repository`, undone if the request is cancelled mid-write.

    The grant write runs in a worker thread, so a cancelled request can return
    before the write lands while the write still lands afterwards, leaving the
    repository grant over local code with nobody to revert it. The switch runs
    as its own task: on cancellation this waits for it to settle and reverts a
    grant it wrote, then lets the cancellation through.
    """
    task = asyncio.ensure_future(switch_to_repository(name, consent, caller=caller))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:

        async def _undo() -> None:
            try:
                result = await task
                if not isinstance(result, SwitchRefusal):
                    await revert_switch(name, result, caller=caller)
            except Exception:  # noqa: BLE001 - the cancellation is what propagates
                logger.warning(
                    "could not settle a cancelled trust switch for %r", name, exc_info=True
                )

        await asyncio.shield(_undo())
        raise
