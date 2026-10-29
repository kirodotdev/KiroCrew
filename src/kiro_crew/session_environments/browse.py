"""The sprite and boxd branches of a session's Files, health and terminal.

``coder/session_browse.py`` owns the browse protocol and the Coder target; this
module resolves a sprite or boxd machine to the same target, names the shells a
local command reaches, and builds their terminal argv.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.session_environment import SessionEnvironmentUnavailable

if TYPE_CHECKING:
    from kiro_crew.coder.session_browse import _Target


async def _resolve_persistent(
    kind: str, section: Any, home: Any, owner: str, profile_id: str, *, wake: bool = True
) -> _Target:
    """The session's sprite or boxd machine, identity-checked. Never creates one,
    and with ``wake=False`` never wakes one either."""
    from kiro_crew.coder.session_browse import WorkspaceNotRunning, _Target

    environments: Any
    if kind == "sprites":
        from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

        environments = SpritesSessionEnvironments(section, home)
    else:
        from kiro_crew.boxd_session.launch import BoxdSessionEnvironments

        environments = BoxdSessionEnvironments(section, home)
    found = await environments.existing(owner, profile_id)
    if found is None:
        raise WorkspaceNotRunning("this session's workspace has not been created yet")
    row, record = found
    if record is None:
        raise SessionEnvironmentUnavailable(
            "this session's workspace was deleted outside Kiro Crew"
        )
    profile = environments.bound_profile(row)
    workdir = environments.session_workdir(profile, row)
    if not workdir:
        # A blank profile folder is the login home its first start records.
        raise WorkspaceNotRunning("this session's workspace has not started yet")
    return _Target(
        await environments.shell(profile, row),
        workdir,
        environments.binding_id(row),
        # Named for the card but never exec'd: an exec would wake the VM.
        stopped=not wake and not environments.record_running(record),
    )


def _command_shell_types() -> tuple[type, ...]:
    """Shells reached by a local command, besides ``coder ssh``."""
    from kiro_crew.boxd_session.shell import BoxdShell

    return (BoxdShell,)


def vm_terminal_argv(target: _Target) -> Optional[list[str]]:
    """argv for a login shell on a sprite or boxd machine; None for a Coder target.

    Opening one is an explicit request, so a sprite or a boxd machine may wake.
    """
    from kiro_crew.sprites_session.shell import SpritesShell

    if isinstance(target.shell, SpritesShell):
        from kiro_crew import platform_compat
        from kiro_crew.sprites_session.terminal import bridge_argv

        if not platform_compat.IS_POSIX:
            raise SessionEnvironmentUnavailable("a Sprites terminal needs a macOS or Linux gateway")
        return bridge_argv(
            target.shell.sprite,
            target.shell.token_secret,
            target.workdir,
            sprite_id=target.shell.resource_id,
        )
    from kiro_crew.boxd_session.shell import BoxdShell

    if isinstance(target.shell, BoxdShell):
        # Opens in the folder Files and the agent use.
        return target.shell.interactive_argv(target.workdir)
    return None
