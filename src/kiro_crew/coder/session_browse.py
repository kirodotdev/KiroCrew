"""Dashboard access to a remote session's workspace over ``coder ssh``, ``boxd
machine exec`` or the Sprites exec API: read-only ``--browse`` (files and git
views), a separate ``--write`` that refuses a stale ``expected``, the browse
mode's read-only ``stats`` (:func:`session_health`), and an interactive shell.
Browsing never autostarts a stopped workspace (a terminal may wake a sprite or a
boxd machine); requests go on stdin. An AWS Fargate session is reached through
its task's bootstrap instead, which never launches a task.
"""

from __future__ import annotations

import asyncio
import difflib
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
from kiro_crew.platform import redact_via_context
from kiro_crew.security import REDACTED_CREDENTIAL_TAG
from kiro_crew.session_environment import SessionEnvironmentUnavailable

_GIT_OPS = frozenset(("git_status", "git_log", "git_diff"))

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


class WorkdirChanged(LookupError):
    """The profile's working folder is not the one the edit was made under."""


@dataclass(frozen=True)
class _Target:
    #: A ``CoderShell``, ``BoxdShell``, ``SpritesShell`` or, for an AWS Fargate
    #: task, a ``FargateShell``.
    shell: Any
    workdir: str
    binding_id: str
    #: A sprite or boxd machine that is not on compute, resolved with
    #: ``wake=False``: the caller reports it stopped instead of exec'ing into it.
    stopped: bool = False


async def _resolve(session_key: str, *, wake: bool = True) -> _Target:
    """The session's workspace target. ``wake=False`` is for a passive probe:
    a sprite or boxd machine that is not running comes back ``stopped`` and
    must not be exec'd, since an exec would wake it (Coder never autostarts)."""
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
    from kiro_crew.coder.session_launch import (
        KIRO_CLOUD_PROVIDER_ID,
        PROVIDER_ID,
        chosen_provider,
    )

    kind = chosen_provider(section, profile_id, recorded_provider) or PROVIDER_ID
    if kind == KIRO_CLOUD_PROVIDER_ID:
        # The sandbox belongs to Kiro's cloud and only its own agent reaches it.
        raise CloudSandboxOnly(
            "this session runs in a Kiro cloud sandbox; its files and shell are reached "
            "only by its own agent, not from Kiro Crew"
        )
    if kind == "fargate":
        return await _resolve_fargate(section, home, owner, profile_id)
    if kind != PROVIDER_ID:
        return await _resolve_persistent(kind, section, home, owner, profile_id, wake=wake)
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


async def _resolve_persistent(
    kind: str, section: Any, home: Any, owner: str, profile_id: str, *, wake: bool = True
) -> _Target:
    """The session's sprite or boxd machine, identity-checked. Never creates one,
    and with ``wake=False`` never wakes one either."""
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
    from kiro_crew.fargate_session.launch import FargateSessionEnvironments

    envs = FargateSessionEnvironments(section, home)
    profile = envs.profile(profile_id)
    shell = await envs.shell_for(owner, profile_id)
    if shell is None:
        raise WorkspaceNotRunning("this session's task is not running")
    row = await asyncio.to_thread(envs.registry.get, owner)
    binding = row.worker_binding if row is not None else ""
    return _Target(shell, str(profile.get("workdir") or ""), binding)


async def _ask(target: _Target, mode: str, request: dict[str, Any]) -> dict[str, Any]:
    # The workdir is operator config, quoted; the request travels on stdin.
    command = (
        f'cd "{WORKER_DIR}" && exec python3 workspace_tools.py '
        f"--root {shlex.quote(target.workdir)} --{mode}"
    )
    payload = json.dumps(request).encode("utf-8")

    async def once() -> bytes:
        if not isinstance(target.shell, (CoderShell, *_command_shell_types())):
            # A shell with no dial command (a sprite's exec API, a Fargate task's
            # bootstrap) runs it itself.
            return await asyncio.wait_for(target.shell.run(command, payload), _BROWSE_TIMEOUT)
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


def _command_shell_types() -> tuple[type, ...]:
    """Shells reached by a local command, besides ``coder ssh``."""
    from kiro_crew.boxd_session.shell import BoxdShell

    return (BoxdShell,)


def _check_path(path: str) -> str:
    if not isinstance(path, str) or len(path) > _MAX_PATH or "\x00" in path:
        raise ValueError("path is invalid")
    return path or "."


def _redact_git_line(line: str) -> str:
    clean = redact_via_context(line)
    if "\n" in clean or "\0" in clean:
        return REDACTED_CREDENTIAL_TAG if line else ""
    return clean


def _redact_git_text(text: str, *, nul_framed: bool) -> str:
    """Run the shared redactor without consuming Git's record framing.

    ``nul_framed`` text (``git status -z``) is redacted one NUL record at a
    time, so a record whose name holds a newline is judged whole. Anything
    else is newline-framed and redacted one line at a time: a credential
    prefix at the end of one line must not swallow the line after it. That
    cannot split a credential, because each ``git_log`` line is one whole
    commit (``%h\\t%an\\t%at\\t%s`` with a one-line subject).
    """
    records = []
    for record in text.split("\0"):
        lines = record.split("\n")
        if not nul_framed:
            records.append("\n".join(_redact_git_line(line) for line in lines))
            continue
        clean = redact_via_context(record).split("\n")
        if len(clean) != len(lines):
            # A multiline credential can consume line breaks. Withhold the
            # record's bodies rather than expose fragments or move records.
            clean = [REDACTED_CREDENTIAL_TAG if body else "" for body in lines]
        records.append("\n".join(clean))
    return "\0".join(records)


_GIT_MODES = frozenset(("100644", "100755", "120000", "160000"))


def _git_name(result: dict[str, Any], key: str, fallback: str) -> str:
    name = redact_via_context(str(result.get(key, fallback)))
    return name.replace("\n", " ").replace("\r", " ")


def _git_quote(name: str) -> str:
    return json.dumps(name, ensure_ascii=False) if any(c in name for c in '\t"\\') else name


def _git_extended_header(result: dict[str, Any], old_name: str, new_name: str) -> str:
    """Git's mode and rename lines; a mode the guest did not name is left out."""
    old_mode, new_mode = (
        result.get(key) if result.get(key) in _GIT_MODES else None
        for key in ("old_mode", "new_mode")
    )
    lines = []
    # The guest names an absent side's mode as null, the binary reply included.
    if result.get("old_mode", "") is None and new_mode:
        lines.append(f"new file mode {new_mode}")
    elif result.get("new_mode", "") is None and old_mode:
        lines.append(f"deleted file mode {old_mode}")
    elif old_mode and new_mode and old_mode != new_mode:
        lines += [f"old mode {old_mode}", f"new mode {new_mode}"]
    if old_name != new_name:
        # No similarity index: git's score is not reproduced, so none is shown.
        lines += [f"rename from {_git_quote(old_name)}", f"rename to {_git_quote(new_name)}"]
    return "".join(line + "\n" for line in lines)


def _withhold_unclosed_pem(body: str) -> str:
    """Mask an unmatched PEM opener through EOF, preserving LF counts and final LF."""
    parts = body.split("\n")
    closed: set[str] = set()
    cut = len(parts)
    # A backwards pass finds matching footers without rescanning each suffix.
    for i in range(len(parts) - 1, -1, -1):
        marker = parts[i].strip()
        if not marker.endswith("-----"):
            continue
        if marker.startswith("-----END "):
            closed.add(marker[9:-5])
        elif marker.startswith("-----BEGIN ") and marker[11:-5] not in closed:
            cut = i
    if cut == len(parts):
        return body
    # The empty split field after a final LF is not an extra source line.
    for i in range(cut, len(parts) - (not parts[-1])):
        parts[i] = REDACTED_CREDENTIAL_TAG
    return "\n".join(parts)


def _git_diff_from_bodies(result: dict[str, Any], path: str) -> dict[str, Any]:
    """Only whole-body masked text reaches the patch builder or the dashboard."""
    view = {
        key: result[key]
        for key in ("root", "git", "truncated", "error_text", "code", "error")
        if key in result
    }
    if not result.get("git"):
        return view
    name = _git_name(result, "path", path)
    old_name = _git_name(result, "old_path", name) if "old_path" in result else name
    a, b = _git_quote("a/" + old_name), _git_quote("b/" + name)
    if result.get("binary"):
        header = f"diff --git {a} {b}\n" + _git_extended_header(result, old_name, name)
        return {**view, "output": header + f"Binary files {a} and {b} differ\n"}
    if result.get("truncated"):
        # An over-cap file gets no hunks, whatever bodies a reply carries: only
        # its mode, rename and new or deleted file lines are shown.
        extended = _git_extended_header(result, old_name, name)
        output = f"diff --git {a} {b}\n" + extended if extended else ""
        return {**view, "truncated": True, "output": output}
    if any(
        key not in result or not isinstance(result[key], (str, type(None)))
        for key in ("old", "new")
    ):
        return {
            **view,
            "git": False,
            "code": "git_failed",
            "error_text": "the remote workspace sent no file bodies",
        }
    old, new = (
        redact_via_context(_withhold_unclosed_pem(result[key] or "")) for key in ("old", "new")
    )

    # Split on LF only: CR, form feed and Unicode separators belong to a line.
    def lines(body: str) -> list[str]:
        parts = body.split("\n")
        return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])

    old_lines, new_lines = lines(old), lines(new)
    extended = _git_extended_header(result, old_name, name)
    header = f"diff --git {a} {b}\n" + extended
    patch = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=a if result["old"] is not None else "/dev/null",
        tofile=b if result["new"] is not None else "/dev/null",
        n=3,
    )
    output = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in patch
    )
    # A mode change or a rename is a difference even when the bodies match.
    return {**view, "output": header + output if output or extended else ""}


async def browse_session(
    session_key: str, op: str, path: str, source: str | None = None
) -> dict[str, Any]:
    """A read-only request: ``list``/``read`` a path, or a git view of the root.

    ``source`` is a renamed file's old path, read only by ``git_diff``.
    """
    if op not in READ_OPS:
        raise ValueError("op must be one of " + ", ".join(READ_OPS))
    request = {"op": "git_bodies" if op == "git_diff" else op, "path": _check_path(path)}
    if op == "git_diff" and source:
        request["from"] = _check_path(source)
    result = await _ask(await _resolve(session_key), "browse", request)
    if op == "git_diff":
        result = await asyncio.to_thread(_git_diff_from_bodies, result, path)
    if op in _GIT_OPS:
        for field in ("output", "error_text"):
            if op == "git_diff" and field == "output":
                continue
            text = result.get(field)
            if isinstance(text, str):
                nul_framed = op == "git_status" and field == "output" and "\0" in text
                result[field] = await asyncio.to_thread(
                    _redact_git_text, text, nul_framed=nul_framed
                )
    return result


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


async def session_health(session_key: str) -> dict[str, Any]:
    """Workspace identity, state and machine metrics for the environment card.

    Never starts compute: a stopped workspace reports ``state: stopped`` and no
    metrics, rather than being woken to be measured.
    """
    target = await _resolve(session_key, wake=False)
    base: dict[str, Any] = {"workspace": target.shell.workspace, "workdir": target.workdir}
    if target.stopped:
        return {**base, "state": "stopped"}
    request = {"op": "stats", "path": "."}
    try:
        stats = await _ask(target, "browse", request)
        if str(stats.get("error", "")).startswith("op must be"):
            # Guest files from an earlier gateway build answer --browse but not
            # ``stats``, as a JSON reply _ask cannot see: install the current
            # files once and ask again, as _ask does for a missing mode.
            await RemoteWorkerLaunch(target.binding_id, target.shell).install()
            stats = await _ask(target, "browse", request)
    except WorkspaceNotRunning:
        return {**base, "state": "stopped"}
    except (SessionEnvironmentUnavailable, RemoteWorkerError, asyncio.TimeoutError) as exc:
        return {**base, "state": "unavailable", "error": str(exc)[:300]}
    if "error" in stats:
        return {**base, "state": "unavailable", "error": str(stats["error"])[:300]}
    memory = stats.get("memory") if isinstance(stats, dict) else None
    if isinstance(memory, dict):
        # Feeds the [RESOURCES] line's workspace clause for this session.
        from kiro_crew.session_environment import remember_remote_memory

        try:
            remember_remote_memory(
                session_key, int(memory["available_bytes"]), int(memory["total_bytes"])
            )
        except (KeyError, TypeError, ValueError):
            pass
    return {**base, "state": "running", "stats": stats}


_AHEAD_RE = re.compile(r"\[(?:[^\]]*\b)?ahead (\d+)")


def summarize_unsaved(status_output: str, *, truncated: bool = False) -> str | None:
    """What ``git status --porcelain=v1 -z --branch`` says would be lost, or None.

    Counts changed and untracked paths, commits ahead of the upstream, and a
    branch with no upstream at all (every commit on it exists only here).
    ``truncated`` marks output cut at the guest's read cap: the record after
    the last separator may be partial, so it is dropped and the file count is
    worded as a lower bound.
    """
    # Accept newline records from workers without NUL status support as well.
    nul = "\0" in status_output
    if truncated:
        status_output = status_output[: status_output.rfind("\0" if nul else "\n") + 1]
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
        bound = "at least " if truncated else ""
        parts.append(f"{bound}{count} uncommitted file{'s' if count != 1 else ''}")
    ahead = _AHEAD_RE.search(head)
    if ahead and int(ahead.group(1)) > 0:
        count = int(ahead.group(1))
        parts.append(f"{count} unpushed commit{'s' if count != 1 else ''}")
    elif head and "..." not in head and not head.startswith(("## No commits yet", "## HEAD")):
        parts.append(f"branch {head[3:].strip()} was never pushed")
    return ", ".join(parts) or None


async def unsaved_work(session_key: str) -> str | None:
    """Unsaved work in a running workspace; None when stopped or unreachable (never woken)."""
    target = await _resolve(session_key, wake=False)
    if target.stopped:
        return None
    try:
        result = await _ask(target, "browse", {"op": "git_status", "path": "."})
    except (WorkspaceNotRunning, SessionEnvironmentUnavailable):
        return None
    if not isinstance(result, dict) or not result.get("git"):
        return None
    return summarize_unsaved(
        str(result.get("output") or ""), truncated=bool(result.get("truncated"))
    )


async def remote_terminal_argv(session_key: str) -> list[str]:
    """argv for a login shell in the workspace (the template sets its directory).

    Opening one is an explicit request, so a sprite or a boxd machine may wake.
    """
    target = await _resolve(session_key)
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
    # Coder never autostarts a stopped workspace here.
    return list(target.shell.interactive_argv())
