"""Dashboard access to a remote session's workspace over ``coder ssh``: read-only
``--browse``, a separate ``--write`` that refuses a stale ``expected``, and an
interactive shell. None autostarts a stopped workspace; requests go on stdin.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

from kiro_crew.coder.client import CoderClientError
from kiro_crew.coder.remote_worker import (
    WORKER_DIR,
    CoderShell,
    RemoteWorkerError,
    RemoteWorkerLaunch,
    _scrubbed_env,
    run_collect,
)
from kiro_crew.session_environment import SessionEnvironmentUnavailable

#: The guest caps a read at 256 KB of text; JSON escaping can roughly double it.
_BROWSE_OUTPUT_MAX = 2 * 1024 * 1024
_BROWSE_TIMEOUT = 45.0
_MAX_PATH = 4096
#: Largest file body the dashboard editor may save (the guest allows more).
MAX_WRITE_BYTES = 1024 * 1024
READ_OPS = ("list", "read", "git_status")


class NotRemote(LookupError):
    """The session runs on this machine: there is no remote workspace."""


class WorkspaceNotRunning(SessionEnvironmentUnavailable):
    """The session's workspace is stopped (or not created yet)."""


class WorkdirChanged(LookupError):
    """The profile's working folder is not the one the edit was made under."""


@dataclass(frozen=True)
class _Target:
    shell: CoderShell
    workdir: str
    binding_id: str


async def _resolve(session_key: str) -> _Target:
    from kiro_crew.coder.registry import WorkspaceBindingRegistry
    from kiro_crew.coder.session_launch import (
        CoderSessionEnvironments,
        SessionEnvironmentChoices,
        resolve_coder_bin,
    )
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home

    home = await asyncio.to_thread(data_home)
    choices = SessionEnvironmentChoices(home / "coder" / "session-choices.json")
    resolved = await asyncio.to_thread(choices.resolve, session_key)
    if resolved is None:
        raise NotRemote(session_key)
    profile_id, owner = resolved
    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    section = config.session_environments
    # Resolved once, off the loop: the PATH search can stall on a slow mount.
    coder_bin = await asyncio.to_thread(resolve_coder_bin)
    provider = CoderSessionEnvironments(section, home, coder_bin=coder_bin)
    profile = provider.profile(profile_id)
    registry = WorkspaceBindingRegistry(home / "coder" / "session-envs.json")
    binding = await asyncio.to_thread(registry.get_by_session, owner)
    if binding is None or not binding.workspace_uuid:
        raise WorkspaceNotRunning("this session's workspace has not been created yet")
    if binding.deployment_id != provider.url:
        raise SessionEnvironmentUnavailable("this session's Coder deployment has changed")
    try:
        client = provider._client()
        workspace = await client.get_workspace(binding.workspace_name)
        # An older Coder lists the owner name, not id: accept it only for the
        # signed-in user whose id this binding recorded, as `prepare` does.
        owner_names: tuple[str, ...] = ()
        if workspace is not None and binding.owner_id and workspace.owner != binding.owner_id:
            name, user_id = await client.current_user()
            owner_names = (name,) if user_id == binding.owner_id else ()
    except CoderClientError as exc:
        raise SessionEnvironmentUnavailable("could not verify the remote workspace") from exc
    # A reusable name is not ownership: verify the live record before any SSH.
    if (
        workspace is None
        or workspace.uuid != binding.workspace_uuid
        or not binding.owner_id
        or workspace.owner not in (binding.owner_id, *owner_names)
        or workspace.template != binding.template
    ):
        raise SessionEnvironmentUnavailable("this session's workspace identity has changed")
    return _Target(
        CoderShell(coder_bin, binding.workspace_name, provider.url),
        profile.workdir,
        binding.binding_id,
    )


async def _ask(target: _Target, mode: str, request: dict[str, Any]) -> dict[str, Any]:
    # The workdir is operator config, quoted; the request travels on stdin.
    command = (
        f'cd "{WORKER_DIR}" && exec python3 workspace_tools.py '
        f"--root {shlex.quote(target.workdir)} --{mode}"
    )
    payload = json.dumps(request).encode("utf-8")

    async def once() -> bytes:
        return await run_collect(
            target.shell.argv(command),
            payload,
            _scrubbed_env(),
            max_output=_BROWSE_OUTPUT_MAX,
            timeout=_BROWSE_TIMEOUT,
        )

    def outdated(out: bytes) -> bool:
        # Every op sent here is one these guest files serve, so an "op must be"
        # refusal can only come from older guest files that know fewer ops.
        try:
            reply = json.loads(out)
        except ValueError:
            return False
        error = reply.get("error") if isinstance(reply, dict) else None
        return isinstance(error, str) and error.startswith("op must be")

    try:
        try:
            out = await once()
            stale = outdated(out)
        except RemoteWorkerError as exc:
            # Guest files predating this mode exit with a usage error naming it.
            if f"--{mode}" not in str(exc) and "No such file" not in str(exc):
                raise
            stale = True
        if stale:
            # Install the current guest files and retry once.
            await RemoteWorkerLaunch(target.binding_id, target.shell).install()
            out = await once()
    except asyncio.TimeoutError as exc:
        raise SessionEnvironmentUnavailable("the remote workspace did not answer") from exc
    except RemoteWorkerError as exc:
        if "must be started" in str(exc) or "not running" in str(exc):
            raise WorkspaceNotRunning("this session's workspace is stopped") from exc
        raise SessionEnvironmentUnavailable(f"could not reach the remote workspace: {exc}") from exc
    try:
        result = json.loads(out)
    except ValueError as exc:
        raise SessionEnvironmentUnavailable("the remote workspace sent an invalid reply") from exc
    if not isinstance(result, dict):
        raise SessionEnvironmentUnavailable("the remote workspace sent an invalid reply")
    return result


def _check_path(path: str) -> str:
    if not isinstance(path, str) or len(path) > _MAX_PATH or "\x00" in path:
        raise ValueError("path is invalid")
    return path or "."


async def browse_session(session_key: str, op: str, path: str) -> dict[str, Any]:
    """A read-only request: ``list``/``read`` a path, or a git view of the root."""
    if op not in READ_OPS:
        raise ValueError("op must be one of " + ", ".join(READ_OPS))
    return await _ask(await _resolve(session_key), "browse", {"op": op, "path": _check_path(path)})


async def write_session_file(
    session_key: str,
    path: str,
    content: str,
    expected: str | None,
    *,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Save ``content`` to ``path``; refused when the file does not hold ``expected``.

    ``workdir`` is the working folder the edit was made under: checked against
    the one target this write resolves, so a profile re-pointed at another folder
    (whose same path is another file) raises ``WorkdirChanged`` before anything
    is written. None skips the check, for a caller that sends no folder.
    """
    path = _check_path(path)
    if path == ".":
        raise ValueError("a file path is required")
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ValueError(f"content must be text of at most {MAX_WRITE_BYTES} bytes")
    request: dict[str, Any] = {"path": path, "content": content}
    if isinstance(expected, str):
        request["expected"] = expected
    target = await _resolve(session_key)
    if workdir is not None and workdir != target.workdir:
        raise WorkdirChanged(session_key)
    return await _ask(target, "write", request)


_AHEAD_RE = re.compile(r"\[(?:[^\]]*\b)?ahead (\d+)")


def summarize_unsaved(status_output: str) -> str | None:
    """What ``git status --porcelain=v1 --branch`` says would be lost, or None:
    changed paths, unpushed commits, or a branch with no upstream."""
    lines = [line for line in status_output.splitlines() if line.strip()]
    if not lines:
        return None
    head, changes = (lines[0], lines[1:]) if lines[0].startswith("## ") else ("", lines)
    parts: list[str] = []
    if changes:
        parts.append(f"{len(changes)} uncommitted file{'s' if len(changes) != 1 else ''}")
    ahead = _AHEAD_RE.search(head)
    if ahead and int(ahead.group(1)) > 0:
        count = int(ahead.group(1))
        parts.append(f"{count} unpushed commit{'s' if count != 1 else ''}")
    elif head and "..." not in head and not head.startswith(("## No commits yet", "## HEAD")):
        parts.append(f"branch {head[3:].strip()} was never pushed")
    return ", ".join(parts) or None


async def unsaved_work(session_key: str) -> str | None:
    """Unsaved work in a running workspace; None when stopped or unreachable (never woken)."""
    target = await _resolve(session_key)
    try:
        result = await _ask(target, "browse", {"op": "git_status", "path": "."})
    except (WorkspaceNotRunning, SessionEnvironmentUnavailable):
        return None
    if not isinstance(result, dict) or not result.get("git"):
        return None
    return summarize_unsaved(str(result.get("output") or ""))


async def remote_terminal_argv(session_key: str) -> list[str]:
    """argv for a login shell in the workspace (the template sets its directory)."""
    target = await _resolve(session_key)
    # Coder never autostarts a stopped workspace here.
    return list(target.shell.interactive_argv())
