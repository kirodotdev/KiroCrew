"""Kiro Crew's tool gate for the Strands harness, loaded as the Strands CLI's project.

Kiro Crew starts ``strands --agent <sealed copy of this file> --acp-server``. In that
mode the CLI sends ``session/request_permission`` only for a Strands interrupt, so
this module makes every Strands ``Agent`` ask: it patches ``Agent.__init__`` BEFORE
any project code runs, so each agent built afterwards -- by this module, by the
operator's own project, by a sub-agent tool -- carries a ``BeforeToolCallEvent`` hook
that raises one interrupt per tool call. The CLI forwards each interrupt to Kiro Crew
and answers it with the approval; anything but an approval cancels the call.

The interrupt carries Kiro Crew's gate envelope (``kiro-crew-gate``): the tool's name
and arguments as this module read them off the Strands event, keyed by the nonce Kiro
Crew put in this process's environment. The nonce, the marker path and the other
``KIROCREW_STRANDS_*`` values are removed from ``os.environ`` on import, so no tool
or shell this agent runs inherits them.

Provider keys Kiro Crew hands this process (``agent.strands_env``) are withheld from
every shell and code child the default local sandbox starts, and the load marker
records a proof of that, taken on a real child.

This file is package data. Kiro Crew verifies its digest and loads a sealed,
read-only copy; it is never imported by Kiro Crew itself.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import sys
import threading
import uuid
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

NONCE = os.environ.pop("KIROCREW_STRANDS_GATE_SESSION", "")
MARKER_PATH = os.environ.pop("KIROCREW_STRANDS_GATE_MARKER", "")
PROJECT = os.environ.pop("KIROCREW_STRANDS_PROJECT", "")
STATE_DIR = os.environ.pop("KIROCREW_STRANDS_STATE_DIR", "") or str(Path(".agent", "sessions"))
WITHHELD = tuple(
    sorted({name for name in os.environ.pop("KIROCREW_STRANDS_WITHHELD", "").split(":") if name})
)
_WITHHELD_UPPER = frozenset(name.upper() for name in WITHHELD)

#: The harness built-ins the bundled project offers. ``programmatic_tool_caller``
#: is left out: it runs model-written code that calls other tools, and those inner
#: calls cannot ask, so under this gate it would only fail.
DEFAULT_BUILTIN_TOOLS = ["shell", "read", "write", "edit", "web_fetch"]

#: Kind for the harness's own built-ins, matching what the CLI's ACP projection
#: puts on their ``tool_call`` frames. Any other tool is ``other``: a custom tool's
#: name is not evidence of what it does.
_BUILTIN_KINDS = {
    "shell": "execute",
    "read": "read",
    "write": "edit",
    "edit": "edit",
    "web_fetch": "fetch",
}


def _tool_identity(event: BeforeToolCallEvent) -> tuple[str, str]:
    """The title and kind Kiro Crew judges this call by.

    An MCP tool is named ``mcp__<server>__<tool>``, the spelling Kiro Crew's rules
    bind to on every harness, from the tool object Strands resolved rather than
    from the prefixed name the model sees.
    """
    name = str(event.tool_use.get("name", ""))
    tool = event.selected_tool
    if isinstance(tool, MCPAgentTool):
        server = getattr(tool.mcp_client, "client_name", None)
        original = getattr(tool.mcp_tool, "name", None)
        if isinstance(server, str) and server and isinstance(original, str) and original:
            return f"mcp__{server}__{original}", "other"
    return name, _BUILTIN_KINDS.get(name, "other")


class AskKiroCrew(HookProvider):
    """Raise one interrupt per tool call; run the call only on an explicit approval."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.ask)

    def ask(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        title, kind = _tool_identity(event)
        envelope = {
            ENVELOPE_MARKER: 1,
            "nonce": NONCE,
            "tool": title,
            "kind": kind,
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
    sys.path.insert(0, str(entry.parent))
    captured: list[tuple[Agent, tuple, dict]] = []
    original_create_harness = strands_harness.create_harness

    def capture_create_harness(*args: Any, **options: Any) -> Agent:
        agent = original_create_harness(*args, **options)
        captured.append((agent, args, options))
        return agent

    strands_harness.create_harness = capture_create_harness
    try:
        spec = importlib.util.spec_from_file_location("kirocrew_strands_user_project", entry)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load the Strands project at {entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
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
    launch model: a model Kiro Crew was asked to launch is refused rather than
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
