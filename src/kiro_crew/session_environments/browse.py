"""The sprite, boxd and Fargate branches of a session's Files, health and terminal.

``coder/session_browse.py`` owns the browse protocol and the Coder target; this
module resolves a sprite or boxd machine, or a Fargate task, to the same target,
names the shells a local command reaches, and builds their terminal argv.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.session_environment import SessionEnvironmentUnavailable

if TYPE_CHECKING:
    from kiro_crew.coder.session_browse import _Target


async def _resolve_persistent(
    kind: str, section: Any, home: Any, owner: str, profile_id: str, *, wake: bool = True
) -> _Target:
    """The session's sprite or boxd machine, identity-checked, or its Fargate task.
    Never creates one, and with ``wake=False`` never wakes one either."""
    from kiro_crew.coder.session_browse import WorkspaceNotRunning, _Target

    if kind == "fargate":
        return await _resolve_fargate(section, home, owner, profile_id)
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


async def _resolve_fargate(section: Any, home: Any, owner: str, profile_id: str) -> _Target:
    """The session's enrolled Fargate task, reached through its bootstrap.

    Never launches a task and makes no AWS call: the HMAC on every bootstrap
    answer is the live proof that the task is the one this gateway enrolled.
    """
    from kiro_crew.coder.session_browse import WorkspaceNotRunning, _Target
    from kiro_crew.fargate_session.launch import FargateSessionEnvironments

    envs = FargateSessionEnvironments(section, home)
    profile = envs.profile(profile_id)
    shell = await envs.shell_for(owner, profile_id)
    if shell is None:
        raise WorkspaceNotRunning("this session's task is not running")
    row = await asyncio.to_thread(envs.registry.get, owner)
    binding = row.worker_binding if row is not None else ""
    return _Target(shell, str(profile.get("workdir") or ""), binding)


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
    from kiro_crew.fargate_session.shell import FargateShell

    if isinstance(target.shell, FargateShell):
        raise SessionEnvironmentUnavailable(
            "an AWS Fargate session has no terminal; its commands run through the "
            "agent's workspace tools"
        )
    return None
