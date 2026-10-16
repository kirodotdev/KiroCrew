"""Dashboard access to a remote session's workspace: files, git, terminal.

A remote session's files live in its guest workspace, so the dashboard's file,
git and terminal views cannot use the gateway's disk or shell. These ask the
guest instead, through the gateway's ``coder ssh`` (never autostarting a stopped
workspace):

* :func:`browse_session` runs ``workspace_tools.py --browse``: ``list``, ``read``
  and read-only git views, with the agent tools' root confinement and caps. It
  has no write or run operation at all.
* :func:`write_session_file` runs the separate ``--write`` mode, the one mutation,
  which refuses a save whose expected text differs from the file's.
* :func:`remote_terminal_argv` is the argv for an interactive shell there.

Requests travel on stdin as JSON, so a path or file body never reaches a shell.
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
READ_OPS = ("list", "read", "git_status", "git_log", "git_diff")


class NotRemote(LookupError):
    """The session runs on this machine: there is no remote workspace."""


class CloudSandboxOnly(SessionEnvironmentUnavailable):
    """The session runs in a Kiro cloud sandbox, which only its own agent reaches.

    Kiro Crew holds no transport into that sandbox, so files, git and a terminal
    are refused here rather than served from the wrong machine.
    """


class WorkspaceNotRunning(SessionEnvironmentUnavailable):
    """The session's workspace is stopped (or not created yet)."""


@dataclass(frozen=True)
class _Target:
    shell: CoderShell
    workdir: str
    binding_id: str
    home: Any


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
    resolved = await asyncio.to_thread(choices.resolve_choice, session_key)
    if resolved is None:
        raise NotRemote(session_key)
    profile_id, owner, recorded_provider = resolved
    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    section = config.session_environments
    from kiro_crew.coder.session_launch import KIRO_CLOUD_PROVIDER_ID, chosen_provider

    if chosen_provider(section, profile_id, recorded_provider) == KIRO_CLOUD_PROVIDER_ID:
        # The sandbox belongs to Kiro's cloud and only its own agent reaches it.
        raise CloudSandboxOnly(
            "this session runs in a Kiro cloud sandbox; its files and shell are reached "
            "only by its own agent, not from Kiro Crew"
        )
    provider = CoderSessionEnvironments(section, home)
    profile = provider.profile(profile_id)
    registry = WorkspaceBindingRegistry(home / "coder" / "session-envs.json")
    binding = await asyncio.to_thread(registry.get_by_session, owner)
    if binding is None or not binding.workspace_uuid:
        raise WorkspaceNotRunning("this session's workspace has not been created yet")
    if binding.deployment_id != provider.url:
        raise SessionEnvironmentUnavailable("this session's Coder deployment has changed")
    try:
        workspace = await provider._client().get_workspace(binding.workspace_name)
    except CoderClientError as exc:
        raise SessionEnvironmentUnavailable("could not verify the remote workspace") from exc
    # A reusable name is not ownership: verify the live record before any SSH,
    # including installation, file writes and interactive terminals.
    if (
        workspace is None
        or workspace.uuid != binding.workspace_uuid
        or not binding.owner_id
        or workspace.owner != binding.owner_id
        or workspace.template != binding.template
    ):
        raise SessionEnvironmentUnavailable("this session's workspace identity has changed")
    return _Target(
        CoderShell(resolve_coder_bin(), binding.workspace_name, provider.url),
        profile.workdir,
        binding.binding_id,
        home,
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

    try:
        try:
            out = await once()
        except RemoteWorkerError as exc:
            # A guest whose files predate this mode (or that no runtime has
            # prepared since): install the current files once and retry.
            if f"--{mode}" not in str(exc) and "No such file" not in str(exc):
                raise
            await RemoteWorkerLaunch(target.binding_id, target.shell, target.home).install()
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
    session_key: str, path: str, content: str, expected: str | None
) -> dict[str, Any]:
    """Save ``content`` to ``path``; refused when the file does not hold ``expected``."""
    path = _check_path(path)
    if path == ".":
        raise ValueError("a file path is required")
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ValueError(f"content must be text of at most {MAX_WRITE_BYTES} bytes")
    request: dict[str, Any] = {"path": path, "content": content}
    if isinstance(expected, str):
        request["expected"] = expected
    return await _ask(await _resolve(session_key), "write", request)


_AHEAD_RE = re.compile(r"\[(?:[^\]]*\b)?ahead (\d+)")


def summarize_unsaved(status_output: str) -> str | None:
    """What ``git status --porcelain=v1 -z --branch`` says would be lost, or None.

    Counts changed and untracked paths, commits ahead of the upstream, and a
    branch with no upstream at all (every commit on it exists only here).
    """
    # Accept newline records from workers without NUL status support as well.
    nul = "\0" in status_output
    lines = status_output.split("\0") if nul else status_output.splitlines()
    lines = [line for line in lines if line]
    if not lines:
        return None
    head, changes = (lines[0], lines[1:]) if lines[0].startswith("## ") else ("", lines)
    count = 0
    index = 0
    while index < len(changes):
        code = changes[index][:2]
        count += 1
        # NUL porcelain puts the destination first, then a separate source path.
        index += 2 if nul and ("R" in code or "C" in code) else 1
    parts: list[str] = []
    if count:
        parts.append(f"{count} uncommitted file{'s' if count != 1 else ''}")
    ahead = _AHEAD_RE.search(head)
    if ahead and int(ahead.group(1)) > 0:
        count = int(ahead.group(1))
        parts.append(f"{count} unpushed commit{'s' if count != 1 else ''}")
    elif head and "..." not in head and not head.startswith(("## No commits yet", "## HEAD")):
        parts.append(f"branch {head[3:].strip()} was never pushed")
    return ", ".join(parts) or None


async def unsaved_work(session_key: str) -> str | None:
    """Uncommitted or unpushed work in a RUNNING session workspace, or None.

    The read never wakes stopped compute (``coder ssh --disable-autostart``): a
    stopped or unreachable workspace is not measured, and None (nothing known)
    is the answer.
    """
    target = await _resolve(session_key)
    try:
        result = await _ask(target, "browse", {"op": "git_status", "path": "."})
    except (WorkspaceNotRunning, SessionEnvironmentUnavailable):
        return None
    if not isinstance(result, dict) or not result.get("git"):
        return None
    return summarize_unsaved(str(result.get("output") or ""))


async def remote_terminal_argv(session_key: str) -> list[str]:
    """argv for an interactive login shell in the session's remote workspace.

    ``coder ssh`` with no command opens a login shell in the agent's directory,
    which the session template sets to the profile's workdir.
    """
    target = await _resolve(session_key)
    # Coder never autostarts a stopped workspace here.
    return list(target.shell.interactive_argv())
