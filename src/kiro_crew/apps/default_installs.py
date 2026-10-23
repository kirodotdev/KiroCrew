"""Install the registry apps a registry marks ``defaultInstall``, once each.

Two switches, held by two different owners, must both be on:

- **The build** opts a registry in: its ``AppsLoader.default_registries()`` row
  carries ``"allowDefaultInstall": true``, and the registry is in force at the
  ``owner`` tier. An operator-configured registry can never opt in, because the
  install records an execution grant on the operator's behalf and only the build
  may vouch for that.
- **The registry** opts an app in: its row in ``app-registry.json`` carries
  ``"defaultInstall": true``. ``"defaultEnabled": false`` installs it disabled.

After the dashboard listener serves, :func:`reconcile_default_installs` fetches
each opted-in registry's index fresh, then installs each marked app that was
never handled before, through the App Store install button's transaction. The
flags are read from that fresh fetch, never from the index cache, which is
agent-writable.

Three more properties carry the design, and each has a test:

- **The grant is the one the consent dialog would record.** It binds the app
  name to the exact repository the fresh row names, in the same
  ``agent.apps_trusted`` / ``apps_trusted_repositories`` keys, so a later
  repository change re-prompts like any other grant. A pre-existing grant to a
  DIFFERENT repository is never overwritten.
- **The row install resolves must be the row the flag came from.** Same registry
  tag and same repository, so a same-named seed, catalog or operator row cannot
  ride on another registry's flag.
- **One attempt that succeeds, then hands off.** Handled names go into
  ``agent.apps_default_installs_applied`` in ``config.json``, the file that
  already holds the grants and that the sandbox seals read-only, so an
  uninstalled or disabled app stays that way. An app already installed is marked
  without being touched. Every new record starts disabled; only guarded enable
  can activate it. A failed or cancelled install withdraws the grant this pass
  added only if no record landed. A landed record keeps its grant and is handed
  off as installed on the next boot; otherwise the next boot retries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.platform import PlatformCompositionError, current_context, safe_context_call
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Upper bound on the apps one boot will act on. The index is reviewed content in
#: an owner-tier registry, so this bounds a listing mistake, not an attacker.
_MAX_DEFAULT_INSTALLS = 32

_APPLIED_KEY = "apps_default_installs_applied"
_ALLOW_KEY = "allowDefaultInstall"


def _allowed_registries() -> list[Any]:
    """Registries in force at the owner tier whose build row sets ``allowDefaultInstall``."""
    from kiro_crew.apps import registry as reg_mod

    empty: list[dict[str, Any]] = []
    rows: Any = safe_context_call(
        lambda: current_context().apps_loader.default_registries(),
        fallback=empty,
        log_message="edition default_registries() unavailable",
    )
    if not rows or isinstance(rows, (str, bytes, dict)) or not hasattr(rows, "__iter__"):
        return []
    allowed = {
        reg_mod._registry_identity_key(row["name"])
        for row in rows
        if isinstance(row, dict)
        and row.get(_ALLOW_KEY) is True
        and isinstance(row.get("name"), str)
        and row["name"].strip()
    }
    if not allowed:
        return []
    pinned = {
        reg_mod._registry_identity_key(p.name or p.repo) for p in reg_mod._pinned_registries()
    }
    out: list[Any] = []
    for reg in reg_mod._effective_registries():
        key = reg_mod._registry_identity_key(reg.name or reg.repo)
        if key not in allowed or key not in pinned:
            continue
        if reg_mod._registry_trust_tier_of(reg) != reg_mod._TRUST_OWNER:
            logger.error(
                "Registry %r allows default installs but is not at the owner tier; ignoring",
                reg_mod._public_registry_name(reg),
            )
            continue
        out.append(reg)
    return out


def _marked_rows(entries: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], bool]]:
    """``(name, row, enabled)`` for each fresh row with ``defaultInstall: true``."""
    from kiro_crew.apps.execution import APP_NAME_RE

    out: list[tuple[str, dict[str, Any], bool]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("defaultInstall") is not True:
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not APP_NAME_RE.fullmatch(name):
            continue
        # Enabled unless the row says exactly ``false``: a typo must not silently
        # flip an app the registry meant to ship on into an off state, and the
        # value cannot widen anything the install itself would not.
        out.append((name, entry, entry.get("defaultEnabled") is not False))
    return out


def _applied_names() -> frozenset[str]:
    """Names already handled: the base ``config.json`` list UNION the effective one.

    The marker is written to the base file, so the base list is what records it.
    The union keeps a ``config.local.json`` list from hiding it: an overlay can
    add names, never take one away, so an uninstalled app cannot come back by an
    overlay replacing the list.

    Raises when the base file cannot be read: "unknown" must mean "install
    nothing", never "nothing was applied", or an unreadable file would reinstall
    apps the operator removed.
    """
    import json

    from kiro_crew.config.loader import KiroCrewConfig, config_path, read_config_text

    names: set[str] = set()
    path = config_path()
    if path.is_file():
        raw = json.loads(read_config_text(path))
        agent_raw = raw.get("agent") if isinstance(raw, dict) else None
        if isinstance(agent_raw, dict):
            names.update(_list(agent_raw, _APPLIED_KEY))
    effective = getattr(KiroCrewConfig.load().agent, _APPLIED_KEY, [])
    if isinstance(effective, list):
        names.update(a for a in effective if isinstance(a, str) and a)
    return frozenset(names)


def _list(agent_raw: dict, key: str) -> list[str]:
    value = agent_raw.get(key)
    return [a for a in value if isinstance(a, str) and a] if isinstance(value, list) else []


async def _write_agent(mutate: Any) -> None:
    """Edit the base ``config.json`` ``agent`` section through the trust writer.

    ``security_trusted_apps._mutate_agent_config`` is the one writer every trust
    grant goes through: the shared config lock, the file lock, the refusal of a
    corrupt file, the refusal when ``config.local.json`` owns a trust key (so a
    grant is never written where it cannot take effect), and the cache
    invalidation. Raises its ``ConfigCorruptError`` / ``TrustSettingOverlayOwned``.
    """
    from kiro_crew.dashboard.handlers.security_trusted_apps import _mutate_agent_config

    await _mutate_agent_config(mutate)


async def _write_agent_marker(mutate: Any) -> None:
    """Edit the base ``agent`` section for a key that is not a trust setting.

    Same locking as :func:`_write_agent` (the shared config lock, the file lock
    and corrupt-file refusal of ``update_config_locked``, the cache invalidation,
    off the event loop), without its refusal when ``config.local.json`` owns a
    trust key: that refusal exists because a base GRANT would be inert, and the
    applied marker is read from the base file, so writing it there always takes
    effect. Raises ``ConfigCorruptError`` on an unreadable file.
    """
    from kiro_crew.config.loader import (
        ConfigReadError,
        _invalidate_config_cache,
        config_path,
        update_config_locked,
    )
    from kiro_crew.dashboard.handlers.agents import _get_config_lock
    from kiro_crew.dashboard.handlers.security_audit import ConfigCorruptError

    def _read_modify_write() -> None:
        path = config_path()

        def _apply(raw: dict) -> dict:
            agent_raw = raw.get("agent")
            if not isinstance(agent_raw, dict):
                agent_raw = {}
                raw["agent"] = agent_raw
            mutate(agent_raw)
            return raw

        try:
            update_config_locked(path, mutate=_apply, stamp_meta=False)
        except ConfigReadError as exc:
            raise ConfigCorruptError(f"{path} is unreadable: {exc}") from exc
        _invalidate_config_cache()

    async with _get_config_lock():
        await asyncio.get_running_loop().run_in_executor(None, _read_modify_write)


async def _mark_applied(name: str) -> None:
    def _mutate(agent_raw: dict) -> None:
        applied = _list(agent_raw, _APPLIED_KEY)
        if name not in applied:
            applied.append(name)
        agent_raw[_APPLIED_KEY] = applied

    await _write_agent_marker(_mutate)


async def _grant(name: str, repository: str) -> None:
    """Record the repository-bound execution grant the consent dialog records.

    Same three keys and the same shape as ``api_trusted_app_grant``'s mutation.
    """
    from kiro_crew.dashboard.handlers.security_trusted_apps import (
        _trusted_list_raw,
        _trusted_local_list_raw,
        _trusted_repositories_raw,
    )

    def _mutate(agent_raw: dict) -> None:
        trusted = _trusted_list_raw(agent_raw)
        if name not in trusted:
            trusted.append(name)
        agent_raw["apps_trusted"] = trusted
        repositories = _trusted_repositories_raw(agent_raw)
        repositories[name] = repository
        agent_raw["apps_trusted_repositories"] = repositories
        agent_raw["apps_trusted_local"] = [
            a for a in _trusted_local_list_raw(agent_raw) if a != name
        ]

    await _write_agent(_mutate)


async def _revoke(name: str, *, drop_name: bool, restore_local: bool) -> None:
    """Undo :func:`_grant` after a failed install. Only called when this pass added it.

    *drop_name* is False when the operator already held a legacy name-only grant
    before this pass, and *restore_local* is True when they held a local-source
    marker that :func:`_grant` removed: only what this pass changed is undone, so
    a failed default install never withdraws consent the operator gave.
    """
    from kiro_crew.dashboard.handlers.security_trusted_apps import (
        _trusted_list_raw,
        _trusted_local_list_raw,
        _trusted_repositories_raw,
    )

    def _mutate(agent_raw: dict) -> None:
        if drop_name:
            agent_raw["apps_trusted"] = [a for a in _trusted_list_raw(agent_raw) if a != name]
        repositories = _trusted_repositories_raw(agent_raw)
        repositories.pop(name, None)
        agent_raw["apps_trusted_repositories"] = repositories
        if restore_local:
            local = _trusted_local_list_raw(agent_raw)
            if name not in local:
                local.append(name)
            agent_raw["apps_trusted_local"] = local

    await _write_agent(_mutate)


def _audit(name: str, outcome: str, detail: str) -> None:
    try:
        sel().log_api_access(
            caller="registry_default_install",
            operation="app_default_install",
            outcome=outcome,
            resources=f"{name}={detail}",
        )
    except Exception:  # noqa: BLE001 - losing the audit line must not change the outcome
        logger.warning("could not audit the default install of %r", name, exc_info=True)


async def _install_transaction(
    name: str, entry: dict[str, Any], *, enabled: bool
) -> dict[str, Any]:
    """Install, then enable the way ``kirocrew app enable`` does.

    Activation goes through ``enable_app``, the guarded transition that applies
    the ``apps`` governance ceiling and the admission policy, and only on its
    success are resources registered and the backend started. Like the CLI
    enable, it does not run ``onEnable`` or load hooks here: the hook reconciler
    loads an enabled app's hooks. Every new record is created disabled, including
    self-managed apps, so a failed or interrupted install cannot bypass enable.
    An app that needs session-approval consent is left suspended and disabled,
    for the owner to enable. A refused enable leaves the app installed and
    disabled, with ``enabled: False`` in the result. With *enabled* False the app
    is installed, disabled, and nothing of it runs.
    *entry* is the row the caller checked; install acts on it rather than looking
    the name up again, so the cache cannot change what runs in between. The
    caller holds ``app_lifecycle_lock(name)``.
    """
    from kiro_crew.apps.manager import enable_app
    from kiro_crew.apps.registry import install_from_registry
    from kiro_crew.apps.routes import (
        _app_may_run_after_install,
        _register_app_off_loop,
        _start_backend_after_install,
        _suspend_app_for_session_approval_reconsent,
    )

    result = await install_from_registry(name, resolved_entry=entry, start_disabled=True)
    if not result.get("ok"):
        return result
    if result.get("needsClientInstall"):
        return result
    if not enabled:
        return {**result, "enabled": False}
    if not await _app_may_run_after_install(result["name"], fresh_install=True):
        await _suspend_app_for_session_approval_reconsent(result["name"])
        return {**result, "enabled": False}
    activated = await asyncio.to_thread(enable_app, result["name"])
    if not activated.ok:
        logger.error("Default install %r: enable refused: %s", name, activated.error)
        return {**result, "enabled": False}
    await _register_app_off_loop(result["name"])
    await _start_backend_after_install(result["name"])
    return result


async def _install_one(name: str, reg: Any, fresh: dict[str, Any], *, enabled: bool) -> str:
    """Handle one marked app. Returns a short outcome tag (logged and tested)."""
    from kiro_crew.apps import registry as reg_mod
    from kiro_crew.apps.execution import (
        builtin_app_names,
        trusted_app_names,
        trusted_app_repository,
        trusted_local_app_names,
    )
    from kiro_crew.apps.manager import app_lifecycle_lock, get_app

    async def _undo_grant() -> None:
        await _revoke(name, drop_name=not had_name_grant, restore_local=had_local)

    async def _undo_grant_unless_landed() -> None:
        if await asyncio.to_thread(get_app, name) is None:
            await _undo_grant()

    if name in await asyncio.to_thread(builtin_app_names):
        logger.error("Default install %r names a built-in app; refusing", name)
        _audit(name, "denied", "refused_builtin")
        return "refused_builtin"

    fresh_url = reg_mod._entry_git_url(fresh)
    if not fresh_url or reg_mod._git_target_is_unsupported(fresh_url):
        _audit(name, "denied", "refused_source")
        return "refused_source"
    repository = reg_mod._normalize_git_target(fresh_url)
    registry = reg_mod._public_registry_name(reg)

    async with app_lifecycle_lock(name):
        if await asyncio.to_thread(get_app, name) is not None:
            # Already installed, by the operator or an earlier build. Hand off
            # without touching its enabled state or its grant.
            await _mark_applied(name)
            return "already_installed"

        # The row install will act on must be the row the flag came from.
        # Same registry tag and byte-identical clone coordinates (name, URL, branch,
        # subdirectory), the comparison ``indexes._owner_tier_confirmed`` makes: the
        # resolver may answer from the on-disk cache, and a cache that differs from
        # the fresh fetch is refused, not installed.
        resolved = await asyncio.to_thread(reg_mod.get_registry_app, name)
        if (
            not resolved
            or resolved.get("_registry") != registry
            or reg_mod._install_coordinates(resolved) != reg_mod._install_coordinates(fresh)
        ):
            logger.error(
                "Default install %r: the row that resolves for this name is not the "
                "one registry %r marked; refusing",
                name,
                registry,
            )
            _audit(name, "denied", "refused_source")
            return "refused_source"

        existing = await asyncio.to_thread(trusted_app_repository, name)
        if existing and existing != repository:
            # The operator consented to a different repository under this name.
            # Never overwrite their decision; leave it unmarked so it is visible.
            logger.error(
                "Default install %r: an existing trust grant names a different "
                "repository; leaving it to the operator",
                name,
            )
            _audit(name, "denied", "refused_existing_grant")
            return "refused_existing_grant"

        added_grant = not existing
        had_name_grant = name in await asyncio.to_thread(trusted_app_names)
        had_local = name in await asyncio.to_thread(trusted_local_app_names)
        if added_grant:
            try:
                await _grant(name, repository)
            except Exception as exc:  # noqa: BLE001 - nothing was written; retry next boot
                # ``TrustSettingOverlayOwned``: config.local.json owns a trust key,
                # so a base grant could not take effect. ``ConfigCorruptError``:
                # the file is unreadable and is left as it is.
                logger.error("Default install %r: could not record the grant: %s", name, exc)
                _audit(name, "denied", "grant_refused")
                return "grant_refused"

        try:
            result = await _install_transaction(name, fresh, enabled=enabled)
        except asyncio.CancelledError:
            # Shutdown mid-install. Undo only an unlanded grant before the task
            # ends; a landed record keeps its repository-bound execution grant.
            if added_grant:
                await asyncio.shield(_undo_grant_unless_landed())
            raise
        except Exception:
            logger.exception("Default install %r raised", name)
            result = {"ok": False}
        if not result.get("ok") or result.get("needsClientInstall"):
            # Withdraw the grant only when nothing landed. A landed record keeps
            # the grant the build vouched for; the next boot hands it off without
            # changing its enabled state.
            if added_grant and await asyncio.to_thread(get_app, name) is None:
                await _undo_grant()
            _audit(name, "failed", "install_failed")
            return "install_failed"

        await _mark_applied(name)
        if enabled and result.get("enabled") is False:
            # Installed, but the owner's policy or consent keeps it off. That is a
            # decision, not a failure: hand off, so it is not reinstalled.
            _audit(name, "completed", "installed_not_enabled")
            return "installed_not_enabled"
        _audit(name, "completed", "installed_enabled" if enabled else "installed_disabled")
        return "installed" if enabled else "installed_disabled"


async def reconcile_default_installs() -> dict[str, str]:
    """Install every marked app not yet handled. Returns name -> outcome.

    Never raises except ``PlatformCompositionError`` (and cancellation): it runs
    as a background task after boot, and a failed install is retried on the next
    boot.
    """
    from kiro_crew.apps import registry as reg_mod

    try:
        registries = await asyncio.to_thread(_allowed_registries)
    except PlatformCompositionError:
        raise
    except Exception:  # noqa: BLE001 - doing nothing is the safe answer
        logger.warning("could not resolve default-install registries", exc_info=True)
        return {}
    if not registries:
        return {}
    try:
        applied = await asyncio.to_thread(_applied_names)
    except PlatformCompositionError:
        raise
    except Exception:  # noqa: BLE001 - doing nothing is the safe answer
        logger.warning("could not read %s; skipping default installs", _APPLIED_KEY)
        return {}

    candidates: list[tuple[str, Any, dict[str, Any], bool]] = []
    seen: set[str] = set()
    for reg in registries:
        # Fresh, never the cache: the cache is agent-writable, and a flag read
        # from it would let a local write choose what gets installed and trusted.
        try:
            entries = await reg_mod._fetch_and_cache_external_registry(reg)
        except PlatformCompositionError:
            raise
        except Exception:  # noqa: BLE001 - treated like a failed fetch
            logger.warning("Default installs: fetch raised", exc_info=True)
            entries = None
        if entries is None:
            logger.warning(
                "Default installs: could not fetch registry %r; retrying next boot",
                reg_mod._public_registry_name(reg),
            )
            continue
        for name, entry, enabled in _marked_rows(entries):
            if name in seen:
                continue
            seen.add(name)
            candidates.append((name, reg, entry, enabled))

    outcomes: dict[str, str] = {}
    pending = [c for c in candidates if c[0] not in applied][:_MAX_DEFAULT_INSTALLS]
    for name, reg, entry, enabled in pending:
        try:
            outcomes[name] = await _install_one(name, reg, entry, enabled=enabled)
        except PlatformCompositionError:
            raise
        except Exception:  # noqa: BLE001 - one app must not stop the others
            logger.exception("Default install %r failed", name)
            outcomes[name] = "error"
        logger.info("Default install %r: %s", name, outcomes[name])
    return outcomes
