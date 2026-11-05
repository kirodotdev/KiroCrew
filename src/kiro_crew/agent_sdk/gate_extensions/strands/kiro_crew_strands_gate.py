"""Kiro Crew's tool gate for the Strands harness, loaded as the Strands CLI's project.

Kiro Crew starts ``strands --agent <sealed copy of this file> --acp-server``. In that
mode the CLI sends ``session/request_permission`` only for a Strands interrupt, so
this module makes every Strands ``Agent`` ask: it patches ``Agent.__init__`` BEFORE
any project code runs, so each agent built afterwards -- by this module, by the
operator's own project, by a sub-agent tool -- carries a ``BeforeToolCallEvent`` hook
that raises one interrupt per tool call. The CLI forwards each interrupt to Kiro Crew
and answers it with the approval; anything but an approval cancels the call.

The interrupt carries Kiro Crew's gate envelope (``kiro-crew-gate``): the tool's name,
its owner (``builtin``, ``custom`` or ``mcp:<server>``, read off the tool OBJECT
Strands resolved), its ``toolUseId`` and its arguments, keyed by the nonce Kiro Crew
put in this process's environment. Kiro Crew classifies the call from the owner and
name only. The nonce, the marker path and the other
``KIROCREW_STRANDS_*`` values are removed from ``os.environ`` on import, so no tool
or shell this agent runs inherits them.

Provider keys Kiro Crew holds for this process (``agent.strands_env``) never arrive in
a launch environment: they come in a one-time owner-only file this module reads into
its own ``os.environ`` and removes before any tool can run, and the worker then makes
itself non-dumpable. They are withheld from every shell and code child the default
local sandbox starts, and the load marker records a proof of both, taken on a real
child.

This file is package data. Kiro Crew verifies its digest and loads a sealed,
read-only copy; it is never imported by Kiro Crew itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import os
import runpy
import stat
import sys
import threading
import uuid
import zipimport
from pathlib import Path
from typing import Any

import strands_harness
from strands import Agent
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent
from strands.sandbox import not_a_sandbox_local_environment as _local_sandbox
from strands.sandbox.types import ExecutionResult
from strands.session import SnapshotSessionManager
from strands.storage import LocalFileStorage
from strands.tools.mcp import MCPAgentTool

GATE_NAME = "kiro-crew-strands-gate"
ENVELOPE_MARKER = "kiro-crew-gate"

#: The key file Kiro Crew writes for this worker, and the prefix of its one-time
#: directory (``acp/harness/strands_keys.py`` on the Kiro Crew side).
_KEYS_FILE_ENV = "KIROCREW_STRANDS_KEYS_FILE"
_KEYS_DIR_PREFIX = "kirocrew-strands-keys-"


class ProviderKeysRefused(RuntimeError):
    """The provider key file is missing, unreadable or not owner-only: do not start."""


def _load_provider_keys(path: str) -> dict[str, str]:
    """Read the provider keys from Kiro Crew's one-time file, then remove it.

    Kiro Crew never puts a provider key in a launch environment, where
    ``/proc/<pid>/environ`` would show it to every shell the agent runs; it writes
    them to an owner-only file and names it here. Anything but a regular,
    non-linked file this account owns, readable by nobody else, refuses the start.
    The file is removed on every outcome, refused or not.
    """
    try:
        return _read_provider_keys(path)
    finally:
        _discard_keys_file(path)


def _read_provider_keys(path: str) -> dict[str, str]:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ProviderKeysRefused(f"the provider key file is unreadable ({exc})") from None
    if not stat.S_ISREG(info.st_mode):
        raise ProviderKeysRefused("the provider key file is not a regular file")
    if os.name == "posix":
        # Looked up, not called by name: this module runs in the Strands project's
        # interpreter, where kiro_crew.platform_compat is not importable, and Windows
        # has no getuid. On POSIX a missing one refuses rather than skips the check.
        own_uid = getattr(os, "getuid", None)
        if own_uid is None or info.st_uid != own_uid():
            raise ProviderKeysRefused("the provider key file belongs to another account")
        if info.st_mode & 0o077:
            raise ProviderKeysRefused("the provider key file is readable by other accounts")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ProviderKeysRefused(f"the provider key file is unreadable ({exc})") from None
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ProviderKeysRefused("the provider key file changed while it was opened")
        try:
            values = json.load(handle)
        except ValueError as exc:
            raise ProviderKeysRefused(f"the provider key file is unreadable ({exc})") from None
    if not isinstance(values, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in values.items()
    ):
        raise ProviderKeysRefused("the provider key file does not hold name/value pairs")
    return values


def _discard_keys_file(path: str) -> None:
    """Remove the key file, and refuse the start when it is still there afterwards.

    A file that outlives this call is plaintext keys the agent's shells could read
    from the scratch window, so "could not remove" is a refusal, never a pass. Already
    gone is success. The empty one-time directory is tidied best-effort: it holds
    nothing.
    """
    unlink_error = ""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        unlink_error = f" ({exc})"
    # Proven gone only by FileNotFoundError: os.path.lexists answers False on ANY
    # lstat error (EACCES, EIO), which would be a false clear.
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ProviderKeysRefused(
            f"the provider key file's removal cannot be confirmed ({exc})"
        ) from None
    else:
        raise ProviderKeysRefused(f"the provider key file could not be removed{unlink_error}")
    directory = os.path.dirname(path)
    if os.path.basename(directory).startswith(_KEYS_DIR_PREFIX):
        try:
            os.rmdir(directory)
        except OSError:
            pass


def _make_non_dumpable() -> str:
    """Stop same-account processes reading this worker's memory; return what was done.

    The keys now live in this process's memory. On Linux, ``PR_SET_DUMPABLE`` 0 makes
    its ``/proc/<pid>`` entries root-owned, so a shell the agent runs cannot read its
    memory or environment. On macOS, ``PT_DENY_ATTACH`` refuses a debugger attach.
    Windows has no equivalent a process can apply to itself; that residual is named
    in the spec.
    """
    try:
        import ctypes

        if sys.platform.startswith("linux"):
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE, 0
                return f"prctl-failed:{ctypes.get_errno()}"
            return "linux-non-dumpable"
        if sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.ptrace(31, 0, None, 0) != 0:  # PT_DENY_ATTACH
                return f"ptrace-failed:{ctypes.get_errno()}"
            return "darwin-deny-attach"
    except (OSError, AttributeError) as exc:
        return f"unavailable:{exc}"
    return "none-on-this-platform"


def _launch_env_holds(pid: int, names: list[str]) -> list[str] | None:
    """Which of *names* the launch environment of *pid* carries, or ``None`` with no ``/proc``."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            entries = handle.read().split(b"\0")
    except OSError:
        return None
    present = {entry.split(b"=", 1)[0].decode("utf-8", "replace") for entry in entries if entry}
    return [name for name in names if name in present]


_KEYS_FILE = os.environ.pop(_KEYS_FILE_ENV, "")
_PROVIDER_KEYS = _load_provider_keys(_KEYS_FILE) if _KEYS_FILE else {}
# A runtime change of this process's environment: ``/proc/<pid>/environ`` keeps
# showing the launch block, which never held these.
os.environ.update(_PROVIDER_KEYS)
# Read BEFORE the worker locks itself: once non-dumpable, even this process cannot
# open its own ``/proc/self/environ``.
_LAUNCH_ENV_HOLDS = _launch_env_holds(os.getpid(), sorted(_PROVIDER_KEYS))
_PARENT_LAUNCH_ENV_HOLDS = _launch_env_holds(os.getppid(), sorted(_PROVIDER_KEYS))
_DUMPABLE = _make_non_dumpable() if _PROVIDER_KEYS else "no-keys"

NONCE = os.environ.pop("KIROCREW_STRANDS_GATE_SESSION", "")
MARKER_PATH = os.environ.pop("KIROCREW_STRANDS_GATE_MARKER", "")
PROJECT = os.environ.pop("KIROCREW_STRANDS_PROJECT", "")
PROJECT_SHA256 = os.environ.pop("KIROCREW_STRANDS_PROJECT_SHA256", "")
STATE_DIR = os.environ.pop("KIROCREW_STRANDS_STATE_DIR", "") or str(Path(".agent", "sessions"))
WITHHELD = tuple(
    sorted({name for name in os.environ.pop("KIROCREW_STRANDS_WITHHELD", "").split(":") if name})
)
_WITHHELD_UPPER = frozenset(name.upper() for name in WITHHELD)

#: The harness built-ins the bundled project offers. ``programmatic_tool_caller``
#: is left out: it runs model-written code that calls other tools, and those inner
#: calls cannot ask, so under this gate it would only fail.
DEFAULT_BUILTIN_TOOLS = ["shell", "read", "write", "edit", "web_fetch"]

#: ``owner`` values of the envelope. Kiro Crew classifies a call from the owner and
#: the name together and never from the name alone, so a project's own tool called
#: ``read`` is not judged as the harness's file reader.
OWNER_BUILTIN = "builtin"
OWNER_CUSTOM = "custom"
OWNER_MCP_PREFIX = "mcp:"
#: The SDK's shell under another name. No Kiro Crew owner, so the call is refused.
OWNER_RENAMED_SDK_SHELL = "renamed-sdk-shell"

#: The harness's own built-in tool OBJECTS, by ``id``: each one ``strands_harness``
#: built in its ``_builtin_tools`` factory under its own slot name. A tool is
#: ``builtin`` only when it is one of these objects; a tool merely NAMED like one
#: is ``custom``. Strong references, so an ``id`` is never reused while recorded:
#: a process serves one session and builds a handful of these per agent.
_BUILTIN_TOOL_OBJECTS: dict[int, object] = {}

_harness_agent_module = importlib.import_module("strands_harness.agent")
_original_builtin_tools = getattr(_harness_agent_module, "_builtin_tools")


def _recording_builtin_tools(*args: Any, **kwargs: Any) -> Any:
    """``strands_harness``'s built-in factory, recording the objects it builds.

    The factory returns ``{tool_name: tool}`` in the order of
    ``BUILTIN_TOOL_NAMES``, one per slot. A tool is recorded only when the mapping
    has exactly one entry per slot and the tool still carries its slot's name: a
    project that renamed a built-in (``{"shell": {"name": "read"}}``) gets that
    tool judged as ``custom``, never as the built-in its new name spells.
    """
    tools = _original_builtin_tools(*args, **kwargs)
    slots = tuple(strands_harness.BUILTIN_TOOL_NAMES)
    if isinstance(tools, dict) and len(tools) == len(slots):
        for slot, tool in zip(slots, tools.values()):
            if getattr(tool, "tool_name", None) == slot:
                _BUILTIN_TOOL_OBJECTS[id(tool)] = tool
    return tools


setattr(_harness_agent_module, "_builtin_tools", _recording_builtin_tools)


def _tool_identity(event: BeforeToolCallEvent) -> tuple[str, str]:
    """The title and owner Kiro Crew judges this call by.

    An MCP tool is named ``mcp__<server>__<tool>``, the spelling Kiro Crew's rules
    bind to on every harness, from the tool object Strands resolved rather than
    from the prefixed name the model sees, and its owner is ``mcp:<server>``. An
    MCP tool whose server or tool name cannot be read is owned by ``mcp:`` with no
    server, which Kiro Crew refuses. A built-in is the object the harness built
    (:data:`_BUILTIN_TOOL_OBJECTS`), titled by its own name. Every other tool is
    ``custom``: its name is not evidence of what it does.
    """
    name = str(event.tool_use.get("name", ""))
    tool = event.selected_tool
    if isinstance(tool, MCPAgentTool):
        server = getattr(tool.mcp_client, "client_name", None)
        original = getattr(tool.mcp_tool, "name", None)
        if isinstance(server, str) and server and isinstance(original, str) and original:
            return f"mcp__{server}__{original}", f"{OWNER_MCP_PREFIX}{server}"
        return name, OWNER_MCP_PREFIX
    if tool is not None and _BUILTIN_TOOL_OBJECTS.get(id(tool)) is tool:
        return str(getattr(tool, "tool_name", name)), OWNER_BUILTIN
    if _is_sdk_shell(tool):
        # The Strands SDK's own shell, built by ``make_shell`` outside the
        # harness factory (``Agent(tools=[shell])``, a sandbox's vended shell, a
        # renamed ``make_shell(name="bash")``). Claiming it is a command can only
        # tighten what Crew checks, so the module is proof enough. Titled by its
        # own name. Renamed (``make_shell(name="read")``), it would wear another
        # built-in's name and kind, so it gets an owner Crew does not know, and
        # Crew refuses the call rather than judge a command as a read.
        title = str(getattr(tool, "tool_name", name) or name)
        return title, (OWNER_BUILTIN if title == "shell" else OWNER_RENAMED_SDK_SHELL)
    return name, OWNER_CUSTOM


#: The module the Strands SDK's ``make_shell`` defines its tool function in.
_SDK_SHELL_MODULE = "strands.vended_tools.shell.shell"


def _is_sdk_shell(tool: object) -> bool:
    """Whether *tool* runs the Strands SDK's own shell function."""
    func = getattr(tool, "_tool_func", None)
    return getattr(func, "__module__", None) == _SDK_SHELL_MODULE


class AskKiroCrew(HookProvider):
    """Raise one interrupt per tool call; run the call only on an explicit approval."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.ask)

    def ask(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        title, owner = _tool_identity(event)
        # No ``kind``: Kiro Crew derives it from ``owner`` and ``tool`` itself.
        # ``toolCallId`` is Strands' ``toolUseId``, the id the CLI puts on this
        # call's ``tool_call`` card, so the request joins its card.
        envelope = {
            ENVELOPE_MARKER: 1,
            "nonce": NONCE,
            "tool": title,
            "owner": owner,
            "toolCallId": str(tool_use.get("toolUseId", "")),
            "input": tool_use.get("input", {}),
        }
        response = event.interrupt(
            title or "tool",
            reason={"method": "confirm", "title": title, "message": json.dumps(envelope)},
        )
        if response is not True:
            event.cancel_tool = f"DENIED: Kiro Crew did not approve {title!r}"


_original_agent_init = Agent.__init__


def _gated_agent_init(self: Agent, *args: Any, **kwargs: Any) -> None:
    _original_agent_init(self, *args, **kwargs)
    self.hooks.add_hook(AskKiroCrew())
    self._kirocrew_gated = True  # type: ignore[attr-defined]


Agent.__init__ = _gated_agent_init  # type: ignore[method-assign]

_original_stream_async = Agent.stream_async
_DENY_PASSES = 3


def _answers_interrupts(prompt: object) -> bool:
    return (
        isinstance(prompt, list)
        and bool(prompt)
        and all(isinstance(item, dict) and "interruptResponse" in item for item in prompt)
    )


async def _gated_stream_async(self: Agent, prompt: Any = None, **kwargs: Any) -> Any:
    """Deny every interrupt a cancelled turn left open, then run *prompt*.

    A cancel that lands while this gate's approval interrupt is open leaves the
    agent saved in interrupt state, and Strands then refuses every later prompt
    (and every reload of that session) with "must resume from interrupt". So before
    any prompt that is not itself the interrupt answers, the open interrupts are
    resumed with a DENY: the gate's hook reads that answer and cancels each call,
    so the tool never runs, and the session takes prompts again.
    """
    state = getattr(self, "_interrupt_state", None)
    if state is not None and getattr(state, "activated", False) and not _answers_interrupts(prompt):
        # The model may answer a denial with another tool call, which asks again;
        # that one is denied too, a bounded number of times.
        for _ in range(_DENY_PASSES):
            denials = [
                {"interruptResponse": {"interruptId": interrupt_id, "response": False}}
                for interrupt_id, interrupt in dict(state.interrupts).items()
                if getattr(interrupt, "response", None) is None
            ]
            if not denials or not getattr(state, "activated", False):
                break
            async for _event in _original_stream_async(self, denials):
                pass
        if getattr(state, "activated", False):
            # Nothing left to answer, or the deny pass paused again: never leave
            # the session unable to take a prompt.
            state.deactivate()
    async for event in _original_stream_async(self, prompt, **kwargs):
        yield event


Agent.stream_async = _gated_stream_async  # type: ignore[method-assign,assignment]

_original_stream_process = _local_sandbox._stream_process


def _without_withheld(env: dict[str, str]) -> dict[str, str]:
    return {name: value for name, value in env.items() if name.upper() not in _WITHHELD_UPPER}


def _stream_process_without_withheld(program: str, args: list[str], **kwargs: Any) -> Any:
    """The local sandbox's one spawn, with every name Kiro Crew withholds removed.

    Every command and code run of the default local sandbox reaches this function:
    on POSIX through ``sh -c`` with no ``env`` (the child would inherit this
    process's whole environment), on Windows with an explicit one. Both get the
    filtered copy.
    """
    env = kwargs.get("env")
    kwargs["env"] = _without_withheld(dict(os.environ) if env is None else env)
    return _original_stream_process(program, args, **kwargs)


_local_sandbox._stream_process = _stream_process_without_withheld  # type: ignore[assignment]


def _require_gated(agent: object) -> Agent:
    if not isinstance(agent, Agent) or getattr(agent, "_kirocrew_gated", False) is not True:
        raise TypeError(
            "Kiro Crew could not attach its tool gate to this Strands project's agent, "
            "so it does not start it. Export a strands Agent as 'agent', built after "
            "import (not unpickled or copied)."
        )
    # Unconditional, not keyed on WITHHELD: every real session withholds the CLI's
    # credential names, so the read-back (which may carry no vault names) must refuse
    # the same project the session would.
    if not isinstance(agent.sandbox, _local_sandbox.NotASandboxLocalEnvironment):
        raise TypeError(
            "This Strands project sets its own sandbox. Kiro Crew keeps provider keys "
            "out of the agent's shells only in the default local sandbox, so it does "
            "not start this project. Remove the custom sandbox."
        )
    return agent


#: The project fingerprint. MUST equal ``kiro_crew.acp.harness.strands_project_pin``'s
#: algorithm byte for byte (a test compares the two); this file imports nothing of
#: Kiro Crew, so it carries its own copy.
_PIN_DOMAIN = b"kirocrew-strands-project-v1\0"
_PIN_MAX_FILES = 5000
_PIN_MAX_BYTES = 64 * 1024 * 1024
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


def _read_regular(path: str, rel: str, limit: int) -> bytes:
    """The bytes of the regular file at *path*, never through a link, or a refusal."""
    with os.fdopen(os.open(path, _READ_FLAGS), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError(f"{rel} is not a regular file")
        if info.st_size > limit:
            raise PermissionError("too many bytes")
        data = handle.read(info.st_size + 1)
    if len(data) != info.st_size:
        raise PermissionError(f"{rel} changed while it was read")
    return data


def _project_fingerprint(
    entry: Path,
) -> tuple[str, dict[str, str], dict[tuple[int, int], str]]:
    """``(sha256, {file: its sha256}, {(device, inode): its sha256})`` over every regular
    file under *entry*'s folder, or :exc:`OSError`. Files are keyed by
    :func:`_lexical_key` and by :func:`_identity`, so a spelling the walk never saw (a
    case alias on a case-insensitive volume, a short name) still finds its file."""
    found: list[tuple[str, str, tuple[int, int]]] = []
    pending: list[tuple[str, str]] = [(str(entry.parent), "")]
    while pending:
        folder, prefix = pending.pop()
        with os.scandir(folder) as entries:
            listed = list(entries)
        for item in listed:
            rel = f"{prefix}{item.name}"
            junction = getattr(item, "is_junction", None)
            if item.is_symlink() or (junction is not None and junction()):
                raise PermissionError(f"{rel} is a link")
            info = os.lstat(item.path)
            mode = info.st_mode
            if stat.S_ISDIR(mode):
                if item.name != "__pycache__":
                    pending.append((item.path, f"{rel}/"))
            elif stat.S_ISREG(mode):
                found.append((rel, item.path, (info.st_dev, info.st_ino)))
                if len(found) > _PIN_MAX_FILES:
                    raise PermissionError("too many files")
            else:
                raise PermissionError(f"{rel} is not a regular file")
    digest = hashlib.sha256(_PIN_DOMAIN)
    files: dict[str, str] = {}
    ids: dict[tuple[int, int], str] = {}
    total = 0
    for rel, path, identity in sorted(found):
        data = _read_regular(path, rel, _PIN_MAX_BYTES - total)
        total += len(data)
        name = rel.encode("utf-8", "surrogateescape")
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        files[_lexical_key(path)] = hashlib.sha256(data).hexdigest()
        if identity[1]:
            ids[identity] = files[_lexical_key(path)]
    return digest.hexdigest(), files, ids


def _identity(path: str, *, follow: bool) -> tuple[int, int] | None:
    """``(device, inode)`` of *path*, or ``None`` when it cannot be read or the volume
    reports no inode (0): such a volume is judged by path alone."""
    try:
        info = os.stat(path) if follow else os.lstat(path)
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino) if info.st_ino else None


def _lexical_key(path: str) -> str:
    """The path as written, made absolute: what the folder walk recorded."""
    return os.path.normcase(os.path.abspath(path))


def _folder_key(path: str) -> str:
    """The path with its FOLDER resolved and its last name kept: a link alias of the
    project folder on ``sys.path`` maps to the recorded file, a link at the file does not."""
    absolute = os.path.abspath(path)
    folder, name = os.path.split(absolute)
    return os.path.normcase(os.path.join(os.path.realpath(folder), name))


class _ConfirmedSource(importlib.machinery.SourceFileLoader):
    """A project ``.py`` file's loader that compiles only bytes matching its confirmed
    digest and never reads bytecode: a cached ``.pyc`` is outside the fingerprint, and
    Python trusts an unchecked-hash ``.pyc`` without looking at its source. Everything
    else (package resources, ``get_data``, ``is_package``) is the ordinary source
    loader's."""

    def __init__(self, fullname: str, path: str, expected: str | None) -> None:
        super().__init__(fullname, path)
        self._expected = expected

    def get_code(self, fullname: str) -> Any:
        return _confirmed_code(self.get_filename(fullname), self._expected)


def _confirmed_code(origin: str, expected: str | None) -> Any:
    """*origin* compiled from bytes matching *expected*, or :exc:`ImportError`."""
    refusal = (
        f"{origin} is not part of the Strands project version confirmed in "
        "agent.strands_project_sha256, so Kiro Crew does not import it. Review "
        "the project, confirm it again, and start a new chat."
    )
    if expected is None or os.path.islink(origin):
        raise ImportError(refusal)
    try:
        data = _read_regular(origin, origin, _PIN_MAX_BYTES)
    except OSError as exc:
        raise ImportError(f"Kiro Crew cannot read {origin} ({exc})") from None
    if hashlib.sha256(data).hexdigest() != expected:
        raise ImportError(refusal)
    return compile(data, origin, "exec", dont_inherit=True)


class _ConfirmedProjectFinder(importlib.abc.MetaPathFinder):
    """Every import that resolves into the project folder, for the whole session, runs
    only a confirmed ``.py`` file through :class:`_ConfirmedSource`.

    "Into the folder" is judged on the path as written, with its folder resolved, and
    fully resolved: a file linked out of the folder, or the folder reached through a
    link, is still the folder's, so it is checked and never handed to Python's own
    loader."""

    def __init__(self, root: str, files: dict[str, str], ids: dict[tuple[int, int], str]) -> None:
        self._roots = {_lexical_key(root), os.path.normcase(os.path.realpath(root))}
        self._root_id = _identity(root, follow=True)
        self._files = files
        self._ids = ids

    def _under(self, key: str) -> bool:
        return any(key == root or key.startswith(root + os.sep) for root in self._roots)

    def _below_root_folder(self, path: str) -> bool:
        """Whether a folder above *path* IS the project folder, by device and inode, so
        any spelling of it counts (letter case, a short name, a link)."""
        if self._root_id is None:
            return False
        folder = os.path.dirname(path)
        while True:
            if _identity(folder, follow=True) == self._root_id:
                return True
            parent = os.path.dirname(folder)
            if parent == folder:
                return False
            folder = parent

    def inside(self, path: str) -> bool:
        path = os.fsdecode(path)
        keys = (_lexical_key(path), _folder_key(path), os.path.normcase(os.path.realpath(path)))
        if any(self._under(key) for key in keys):
            return True
        return any(
            self._below_root_folder(spelling)
            for spelling in (os.path.abspath(path), os.path.realpath(path))
        )

    def expected(self, path: str) -> str | None:
        """The confirmed digest of the project file at *path*, or ``None``."""
        path = os.fsdecode(path)
        found = self._files.get(_lexical_key(path)) or self._files.get(_folder_key(path))
        if found is None:
            identity = _identity(path, follow=False)
            found = self._ids.get(identity) if identity is not None else None
        return found

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not spec.origin or not spec.has_location:
            return None
        if not self.inside(spec.origin):
            return None
        if not spec.origin.endswith(".py"):
            raise ImportError(
                f"{spec.origin} is not Python source; Kiro Crew imports only confirmed "
                ".py files from the Strands project folder."
            )
        loader = _ConfirmedSource(name, spec.origin, self.expected(spec.origin))
        return importlib.util.spec_from_file_location(
            name,
            spec.origin,
            loader=loader,
            submodule_search_locations=spec.submodule_search_locations,
        )


def _guard_file_loaders(finder: _ConfirmedProjectFinder) -> None:
    """Hold Python's own file loaders to the same rule inside the project folder.

    A project may load a file by path (``spec_from_file_location``, ``runpy``), which
    never asks the meta path. So inside the folder the stock source loader compiles
    only confirmed bytes and never reads bytecode, and the bytecode and extension
    loaders refuse; outside the folder each behaves exactly as before.
    """
    machinery = importlib.machinery
    source_get_code = machinery.SourceFileLoader.get_code
    sourceless_get_code = machinery.SourcelessFileLoader.get_code
    extension_create = machinery.ExtensionFileLoader.create_module

    def refusal(path: str) -> ImportError:
        return ImportError(
            f"{path} is not Python source; Kiro Crew runs only confirmed .py files "
            "from the Strands project folder."
        )

    def get_source_code(self: Any, fullname: str) -> Any:
        path = self.get_filename(fullname)
        if finder.inside(path):
            return _confirmed_code(path, finder.expected(path))
        return source_get_code(self, fullname)

    def get_sourceless_code(self: Any, fullname: str) -> Any:
        path = self.get_filename(fullname)
        if finder.inside(path):
            raise refusal(path)
        return sourceless_get_code(self, fullname)

    def create_extension(self: Any, spec: Any) -> Any:
        if finder.inside(self.path):
            raise refusal(self.path)
        return extension_create(self, spec)

    machinery.SourceFileLoader.get_code = get_source_code  # type: ignore[method-assign]
    machinery.SourcelessFileLoader.get_code = get_sourceless_code  # type: ignore[method-assign]
    machinery.ExtensionFileLoader.create_module = create_extension  # type: ignore[method-assign]

    # ``runpy.run_path`` on a file reads it without a loader. Its helper takes
    # ``(run_name, fname)`` and answers ``(code, fname)`` on older releases, and
    # takes ``(fname)`` and answers ``code`` on newer ones; the shape is kept per call.
    run_file = runpy._get_code_from_file  # type: ignore[attr-defined]

    def get_run_file_code(*args: Any) -> Any:
        fname = args[-1]
        path = os.fsdecode(fname)
        if not finder.inside(path):
            return run_file(*args)
        if not path.endswith(".py"):
            raise refusal(path)
        code = _confirmed_code(path, finder.expected(path))
        return (code, fname) if len(args) == 2 else code

    runpy._get_code_from_file = get_run_file_code  # type: ignore[attr-defined]

    # A zip archive in the folder is never imported from: its members are not files
    # the fingerprint can check one by one at import time.
    zip_init = zipimport.zipimporter.__init__

    def zip_importer(self: Any, path: Any, *args: Any) -> None:
        if finder.inside(os.fspath(path)):
            raise zipimport.ZipImportError(f"{path} is a zip archive in the Strands project")
        zip_init(self, path, *args)

    zipimport.zipimporter.__init__ = zip_importer  # type: ignore[method-assign]


def _require_confirmed_project(
    entry: Path,
) -> tuple[dict[str, str], dict[tuple[int, int], str]]:
    """The per-file digests of the confirmed project, or a refusal.

    Kiro Crew checked the same fingerprint before it started this process; this is the
    same check right before the import, so a file swapped in between is not run.
    """
    try:
        current, files, ids = _project_fingerprint(entry)
    except OSError as exc:
        current, files, ids = f"unreadable ({exc})", {}, {}
    if not PROJECT_SHA256 or current != PROJECT_SHA256:
        raise PermissionError(
            "The Strands project's files are not the version confirmed in "
            "agent.strands_project_sha256, so Kiro Crew does not run them. Review the "
            "project, confirm it again, and start a new chat."
        )
    return files, ids


def _load_user_project(path: str) -> tuple[Any, list[tuple[Agent, tuple, dict]]]:
    """Import the operator's project module with every Agent it builds gated."""
    target = Path(path).expanduser()
    entry = target / "agent.py" if target.is_dir() else target
    if not entry.is_file() or entry.suffix != ".py":
        raise FileNotFoundError(
            f"agent.strands_project names {str(target)!r}, which is not a Strands "
            "Python project: point it at the project's agent.py or its folder."
        )
    entry = entry.resolve()
    files, ids = _require_confirmed_project(entry)
    # Just ahead of the path finder, before the folder joins sys.path: from here on,
    # any import that lands in the folder (now or later in the session) is checked.
    # Built-in and frozen modules still resolve first, as they always do.
    path_finder = importlib.machinery.PathFinder
    at = next((i for i, f in enumerate(sys.meta_path) if f is path_finder), 0)
    finder = _ConfirmedProjectFinder(str(entry.parent), files, ids)
    sys.meta_path.insert(at, finder)
    _guard_file_loaders(finder)
    # An importer cached for a path in the folder before the guard existed (a zip on
    # PYTHONPATH) would keep serving it unchecked; the next lookup builds a new one.
    for cached in [key for key in sys.path_importer_cache if finder.inside(str(key))]:
        del sys.path_importer_cache[cached]
    sys.path.insert(0, str(entry.parent))
    captured: list[tuple[Agent, tuple, dict]] = []
    original_create_harness = strands_harness.create_harness

    def capture_create_harness(*args: Any, **options: Any) -> Agent:
        agent = original_create_harness(*args, **options)
        captured.append((agent, args, options))
        return agent

    strands_harness.create_harness = capture_create_harness
    try:
        name = "kirocrew_strands_user_project"
        loader = _ConfirmedSource(name, str(entry), files.get(_lexical_key(str(entry))))
        spec = importlib.util.spec_from_file_location(name, entry, loader=loader)
        if spec is None:
            raise ImportError(f"cannot load the Strands project at {entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        loader.exec_module(module)
    finally:
        strands_harness.create_harness = original_create_harness
    return module, captured


def _drop_interrupted_import_turn(agent: object) -> None:
    """Forget a turn the project's own module code started and the gate stopped.

    A script-style project may call its agent at import. Its first tool call then
    raised this gate's interrupt with nobody to answer it, which leaves the agent
    paused on that interrupt with an unanswered tool call in its history; Strands
    refuses every later prompt in that state. The call never ran, so the paused
    turn is dropped and the agent starts the session empty.
    """
    state = getattr(agent, "_interrupt_state", None)
    if state is not None and getattr(state, "activated", False):
        state.deactivate()
        agent.messages.clear()  # type: ignore[attr-defined]


def _serve_exported_instance(agent: Agent, session_id: object, overrides: dict[str, Any]) -> Agent:
    """The project's own ``Agent`` instance, given the per-session persistence the CLI
    reads, or a refusal.

    A plain ``agent = Agent(...)`` has no factory, so nothing can rebuild it with a
    launch model or effort: one Kiro Crew was asked to launch is refused rather than
    silently ignored. Without a session manager the CLI's worker saves nothing, so a
    later ``session/load`` would answer success on an empty agent. The instance gets
    the same ``SnapshotSessionManager`` the harness's own factory builds, under the
    state directory Kiro Crew names, attached the way ``Agent.__init__`` attaches one
    and restored through its public ``initialize``. One worker process serves one
    session, so the one instance is that session's agent.
    """
    if overrides.get("model") is not None:
        raise ValueError(
            "agent.strands_project exports an Agent instance with no create_agent(**overrides), "
            "so it cannot run another model. Clear the chat model and agent.strands_model, or "
            "give the project a create_agent(**overrides) factory."
        )
    if overrides.get("effort") not in (None, "auto"):
        # Same reason: the instance keeps the effort its own code set, so a launch
        # level would be reported as applied while the agent ignores it.
        raise ValueError(
            "agent.strands_project exports an Agent instance with no create_agent(**overrides), "
            "so it cannot run another reasoning effort. Set the chat's effort to auto and unset "
            "agent.reasoning_effort and any crew or role effort it inherits, or give the "
            "project a create_agent(**overrides) factory."
        )
    _drop_interrupted_import_turn(agent)
    if getattr(agent, "_session_manager", None) is None:
        manager = SnapshotSessionManager(
            session_id if isinstance(session_id, str) and session_id else uuid.uuid4().hex[:8],
            storage=LocalFileStorage(STATE_DIR),
            save_latest_on="message",
        )
        agent._session_manager = manager  # type: ignore[attr-defined]
        agent._session_id = manager.session_id  # type: ignore[attr-defined]
        agent.hooks.add_hook(manager)
        manager.initialize(agent)
    return agent


def _gated_and_marked(agent: object) -> Agent:
    gated = _require_gated(agent)
    _write_marker()
    return gated


async def _await_gated(pending: Any) -> Agent:
    """An ``async def create_agent`` project factory: await it, then check the gate."""
    return _gated_and_marked(await pending)


def _session_option(session_id: object) -> dict[str, str]:
    return (
        {"id": session_id, "dir": STATE_DIR} if isinstance(session_id, str) else {"dir": STATE_DIR}
    )


_user_module, _user_captured = (None, []) if not PROJECT else _load_user_project(PROJECT)


def create_agent(**overrides: Any) -> Any:
    """The session's agent, built the way the Strands CLI asks a project for one.

    The CLI passes ``session_id`` / ``session_dir`` and any launch overrides
    (``model``). Sessions are stored under the directory Kiro Crew names, never in
    the working directory.
    """
    session_id = overrides.pop("session_id", None)
    overrides.pop("session_dir", None)
    if _user_module is None:
        agent = strands_harness.create_harness(
            builtin_tools=DEFAULT_BUILTIN_TOOLS,
            background_tasks=False,
            memory=False,
            skills=False,
            session=_session_option(session_id),
            **overrides,
        )
    elif callable(getattr(_user_module, "create_agent", None)):
        if session_id is not None:
            overrides["session_id"] = session_id
            overrides["session_dir"] = STATE_DIR
        built_agent = _user_module.create_agent(**overrides)
        if inspect.isawaitable(built_agent):
            # The CLI's worker awaits an awaitable factory result; the gate check
            # waits for the agent rather than judging the coroutine.
            return _await_gated(built_agent)
        agent = built_agent
    else:
        exported = getattr(_user_module, "agent", None)
        built = next((entry for entry in _user_captured if entry[0] is exported), None)
        if built is not None:
            _agent, args, options = built
            agent = strands_harness.create_harness(
                *args, **{**options, "session": _session_option(session_id), **overrides}
            )
        else:
            # An agent instance with no factory: the one agent this worker serves.
            agent = _serve_exported_instance(_require_gated(exported), session_id, overrides)
    return _gated_and_marked(agent)


def _may_still_exist(path: str) -> bool:
    """False only when *path* is proven gone (FileNotFoundError); any other error is True."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _keys_proof() -> dict[str, Any]:
    """How the provider keys reached this worker, for Kiro Crew to judge.

    ``loaded``: the names read from the one-time file. ``file_left``: whether the
    file still exists (it must not). ``launch_env`` / ``parent_launch_env``: which of
    those names this worker's and the Node CLI's launch environments carry (they must
    carry none), or ``None`` where there is no ``/proc``. ``dumpable``: what
    :func:`_make_non_dumpable` did.
    """
    return {
        "loaded": sorted(_PROVIDER_KEYS),
        "file_left": bool(_KEYS_FILE) and _may_still_exist(_KEYS_FILE),
        "launch_env": _LAUNCH_ENV_HOLDS,
        "parent_launch_env": _PARENT_LAUNCH_ENV_HOLDS,
        "dumpable": _DUMPABLE,
    }


def _child_env_proof() -> dict[str, Any]:
    """Which withheld names a real child of the local sandbox's environment still sees."""
    proof: dict[str, Any] = {
        "names": list(WITHHELD),
        "parent_missing": [name for name in WITHHELD if name not in os.environ],
        "child_visible": [],
        "error": None,
    }
    if not WITHHELD:
        return proof
    probe = "import json, os, sys; print(json.dumps([n for n in sys.argv[1:] if n in os.environ]))"

    async def run() -> str:
        # Through the module attribute the sandbox itself calls, with no env, the
        # way a POSIX shell command reaches it.
        result = None
        async for item in _local_sandbox._stream_process(
            sys.executable, ["-c", probe, *WITHHELD], timeout=30
        ):
            result = item
        if not isinstance(result, ExecutionResult) or result.exit_code != 0:
            raise RuntimeError("the probe child did not exit cleanly")
        return result.stdout

    outcome: dict[str, Any] = {}

    def in_thread() -> None:
        # Its own loop: the CLI builds the agent from inside a running one.
        try:
            outcome["stdout"] = asyncio.run(run())
        except Exception as exc:  # noqa: BLE001 -- any failure is reported, never passed
            outcome["error"] = f"{type(exc).__name__}: {exc}"[:200]

    worker = threading.Thread(target=in_thread, daemon=True)
    worker.start()
    worker.join(60)
    if "stdout" in outcome:
        try:
            visible = json.loads(outcome["stdout"].strip() or "[]")
            proof["child_visible"] = [str(name) for name in visible]
        except ValueError:
            proof["error"] = "the probe child's output was not readable"
    else:
        proof["error"] = outcome.get("error", "the probe child did not finish")
    return proof


_marker_written = False


def _write_marker() -> None:
    """Publish the load marker once, when Kiro Crew named a path for it."""
    global _marker_written
    if not MARKER_PATH or _marker_written:
        return
    marker = {
        "plugin": GATE_NAME,
        "nonce": NONCE,
        "module": os.path.realpath(__file__),
        "child_env": _child_env_proof(),
        "keys": _keys_proof(),
    }
    staging = f"{MARKER_PATH}.{os.getpid()}.tmp"
    with open(staging, "w", encoding="utf-8") as handle:
        json.dump(marker, handle)
    os.replace(staging, MARKER_PATH)
    _marker_written = True


if _user_module is None:
    agent = strands_harness.create_harness(
        builtin_tools=DEFAULT_BUILTIN_TOOLS,
        background_tasks=False,
        memory=False,
        skills=False,
        session=False,
    )
else:
    agent = _require_gated(getattr(_user_module, "agent", None))
