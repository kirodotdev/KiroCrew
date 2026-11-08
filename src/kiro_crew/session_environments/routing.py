"""Which provider a session runs on, and the guard that keeps it there.

A profile id names exactly one provider (Coder, Sprites or boxd). Launch, the
picker, the choice lock and browse resolve it here, and a session whose id now
names another provider than the one it started on is refused rather than moved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.config.session_environments import PROVIDERS
from kiro_crew.session_environment import SessionEnvironmentUnavailable

if TYPE_CHECKING:
    from kiro_crew.coder.session_launch import PreparedRemoteSession

#: ``coder.session_launch.PROVIDER_ID``; not imported, since that module imports this one.
_CODER = "coder"


def _provider_ids_of(section: Any) -> dict[str, set[str]]:
    """Each provider's profile ids in the loaded ``section`` (Coder's are dataclasses)."""
    out: dict[str, set[str]] = {
        _CODER: {
            p_id
            for p in getattr(section, "profiles", None) or []
            if isinstance(p_id := getattr(p, "id", None), str)
        }
    }
    for provider, key, _label in PROVIDERS:
        if provider != _CODER:
            out[provider] = _dict_profile_ids(section, key)
    return out


def ambiguous_profile_ids(section: Any) -> set[str]:
    """Ids that name profiles of more than one provider in the loaded ``section``.

    The config writer refuses that; a hand-edited file can still hold one. Such
    an id names no provider: the picker omits it and a launch refuses it.
    """
    seen: set[str] = set()
    shared: set[str] = set()
    for ids in _provider_ids_of(section).values():
        shared |= seen & ids
        seen |= ids
    return shared


def profile_provider(section: Any, profile_id: str) -> Optional[str]:
    """The provider ``profile_id`` names in ``section``, or None when no profile has it.

    The one resolver the launch path, the picker and the choice lock share.
    Raises :class:`SessionEnvironmentUnavailable` for an id that names profiles
    of more than one provider, rather than picking one of them.
    """
    if profile_id in ambiguous_profile_ids(section):
        raise SessionEnvironmentUnavailable(
            f"profile {profile_id!r} is saved for more than one provider; "
            "give each profile its own id in Settings"
        )
    for provider, ids in _provider_ids_of(section).items():
        if profile_id in ids:
            return provider
    return None


def chosen_provider(section: Any, profile_id: str, recorded: str) -> Optional[str]:
    """The provider a session bound to ``profile_id`` runs on, checked against its row.

    ``recorded`` is the provider the choice row was written with. An id that now
    names a profile of ANOTHER provider (the original deleted, the id reused) is
    refused rather than moving the session there: its files or its history live
    with the provider it started on. None when no profile has the id.
    """
    provider = profile_provider(section, profile_id)
    if provider is not None and provider != recorded:
        raise SessionEnvironmentUnavailable(
            f"profile {profile_id!r} now names a {provider} profile, but this session "
            f"runs on {recorded}; choose its environment again or start a new chat"
        )
    return provider


async def prepare_vm_session(
    section: Any,
    home: Path,
    session_key: str,
    owner_key: str,
    profile_id: str,
    recorded_provider: str,
) -> Optional[PreparedRemoteSession]:
    """Prepare the session's sprite or boxd machine; None when it runs on Coder.

    Raises :class:`SessionEnvironmentUnavailable` for a session whose profile
    was removed or now belongs to another provider.
    """
    # The id names the provider, which must be the one the row recorded; an id
    # saved for two providers is refused.
    chosen = chosen_provider(section, profile_id, recorded_provider)
    # An id no list holds goes down the Coder path, which reports it as not
    # configured. Keyed by the owner, so a subagent reuses its root's VM.
    provider = chosen or _CODER
    # A row written for one provider never runs on another: a profile removed
    # and its id reused by another provider would otherwise move the session
    # onto a fresh, empty VM. A "coder" tag is also what every row carried
    # before other providers existed, so whatever the tag says, a session that
    # already owns a workspace or VM with another provider is pinned to it:
    # moving it would skip that provider's resume check and leave its archived
    # files due for deletion.
    moved = recorded_provider != _CODER and recorded_provider != provider
    if not moved:
        moved = await asyncio.to_thread(_owned_elsewhere, home, owner_key, provider)
    if moved and not _profile_configured(section, profile_id):
        # Removed, not moved: no list holds the id, so there is nothing to look for.
        raise SessionEnvironmentUnavailable(
            f"this session's profile {profile_id!r} is no longer configured; "
            "add it again in Settings > Session Environments"
        )
    if moved:
        raise SessionEnvironmentUnavailable(
            f"this session's profile {profile_id!r} now belongs to another provider; "
            "restore the original profile in Settings > Session Environments"
        )
    vm_environments = _vm_environments().get(provider)
    if vm_environments is None:
        return None
    prepared: PreparedRemoteSession = await vm_environments(section, home).prepare(
        owner_key, profile_id, progress_key=session_key
    )
    return prepared


def _vm_environments() -> dict[str, Any]:
    """Each VM provider's environments class, keyed by its ``VM_PROVIDERS`` id.

    The one place a VM provider is wired to its class: launch dispatches through
    it and the move guard reads every registry it names, so a provider added
    here is covered by both. A test checks it matches ``VM_PROVIDERS``.
    """
    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

    return {"sprites": SpritesSessionEnvironments, "boxd": BoxdSessionEnvironments}


def _profile_configured(section: Any, profile_id: str) -> bool:
    """Whether any provider's list in ``section`` holds ``profile_id``."""
    return any(profile_id in ids for ids in _provider_ids_of(section).values())


def _has_coder_binding(home: Path, owner_key: str) -> bool:
    """Whether ``owner_key`` already owns a Coder workspace row (any state).

    Never creates the registry or its lock on a gateway that has no Coder rows;
    an unreadable registry raises, so the start fails closed.
    """
    from kiro_crew.coder.registry import WorkspaceBindingRegistry

    path = home / "coder" / "session-envs.json"
    if not path.exists():
        return False
    return WorkspaceBindingRegistry(path).get_by_session(owner_key) is not None


def _owned_elsewhere(home: Path, owner_key: str, provider: str) -> bool:
    """Whether ``owner_key`` owns a workspace or VM row with a provider other
    than ``provider`` (any state, an archived one included).

    Reads only: a registry file that is absent is not created. An unreadable
    one raises, so the start fails closed.
    """
    from kiro_crew.session_environments.persistent_vm import SessionResourceRegistry

    if provider != _CODER and _has_coder_binding(home, owner_key):
        return True
    for name, cls in _vm_environments().items():
        if name == provider:
            continue
        registry = SessionResourceRegistry(home / cls.registry_file, cls.label)
        if registry.get(owner_key) is not None:
            return True
    return False


def _dict_profile_ids(section: Any, key: str) -> set[str]:
    return {
        str(p.get("id"))
        for p in (getattr(section, key, None) or [])
        if isinstance(p, dict) and p.get("id")
    }
