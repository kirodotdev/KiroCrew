"""Which tools one channel turn may run, and the tool-less turn an untrusted sender gets.

The shared channel turn pipeline (:mod:`kiro_crew.messaging.dispatch`) hands its
:class:`~kiro_crew.messaging.driver.TurnDriver` the PreToolUse gate built here
(:func:`build_tool_gate`, or the reason-less form a dispatcher's ``Drift`` asks for) and the
spawn auto-approve rung (:func:`build_auto_approve`). A ``deny_all_tools`` turn runs on
:data:`TOOLLESS_TURN_AGENT`, whose spec mounts no tools, in a per-session work directory no
project spec can shadow; where the backend or the session's agent binding cannot make it
tool-less the turn is refused (:class:`ToollessTurnUnavailable`) with
:data:`TOOLLESS_TURN_REFUSAL_NOTE`. Callers import these names from
:mod:`kiro_crew.messaging.dispatch`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from kiro_crew.agent_sdk.backends import Routing, routing_for
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, event_is_spawn_run, hook_gate_kwargs
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.messaging.dispatch")


#: The agent a ``deny_all_tools`` turn runs on. Its spec declares ``tools: []``
#: and no MCP servers (``agent._install_guest_agent``), so the backend mounts
#: nothing for the session: no tool exists to call, whatever the operator's own
#: agent auto-approves. That is the only enforcement that holds on the stock kiro
#: backend, where a tool named in ``allowedTools`` raises no permission request
#: and therefore never reaches the driver's ``deny_all_tools`` branch. A spec of
#: its own, not the background ``kirocrew-lite``: that helper may grow a tool one
#: day and its empty prompt reads as "no user to address" on backends that need
#: one, while this agent talks to a person. The spec pin in
#: ``test_messaging_dispatch.py::TestToollessAgentSpecIsTheBoundary`` makes any
#: drift loud.
TOOLLESS_TURN_AGENT = "kirocrew-guest"


#: What a sender whose turn was refused as un-tool-less-able reads. One line, no
#: internals: silence reads as the agent ignoring the person, and the operator's
#: side of the story is the SEL row, not this note.
TOOLLESS_TURN_REFUSAL_NOTE = "This account cannot answer you on its current setup. Ask its owner."


def toolless_turns_supported(backend: str) -> bool:
    """Whether a ``deny_all_tools`` turn can be driven on *backend* at all.

    True only where the agent spec is what the harness mounts
    (``Routing.AGENT_SPEC``): there ``tools: []`` removes every tool. Read by
    channel startup to warn an operator whose configuration admits non-operator
    traffic on a backend that will refuse every such turn.
    """
    return routing_for(backend) is Routing.AGENT_SPEC


def warn_if_toolless_turns_unservable(
    channel: str, *, admits_non_operators: bool, backend: str, admission: str
) -> bool:
    """Warn once, at a channel's start, when its admitted non-operators will all be refused.

    Shared with every ``deny_all_tools`` adopter because the refusal itself lives
    on the shared seam (:class:`ChannelTurns`); a channel supplies only the two facts
    it alone knows, whether its configuration admits anyone but the operator and
    how (``admission``, for the message). Returns True when it warned.
    """
    if not admits_non_operators or toolless_turns_supported(backend):
        return False
    logger.warning(
        "%s: non-operator senders are admitted (%s) but agent.acp_backend=%r cannot "
        "run a tool-less turn; every non-operator turn will be refused with a note. "
        "Use the kiro backend or narrow admission.",
        channel,
        admission,
        backend,
    )
    return True


class ToollessTurnUnavailable(RuntimeError):
    """A ``deny_all_tools`` turn cannot be made tool-less on this session.

    Two causes. The session key a channel hands in for such a turn must be one
    that only tool-less turns ever use; a key shared with the operator's own
    turns would hand the sender the operator's agent, tools included. And the
    tool-less agent spec is honoured only by a backend whose routing is
    ``Routing.AGENT_SPEC`` (the spawn names the agent, so ``tools: []`` is what
    the harness mounts); a harness that reads no agent spec keeps its own native
    tools, and a project-preapproved one raises no permission request for the
    driver to refuse. Raised instead of running the turn either way: refusing
    costs the sender one reply, running it costs the operator their machine.
    """


def build_tool_gate(ctx_builder: Any, *, session_key: str, agent: str) -> Callable[[Any], str]:
    """PreToolUse security gate, channel-neutral (off ``ctx_builder.hooks``).

    Sensitive-path keystone + governance ceiling + deny-list. Returns ``"deny"``
    (un-overridable), ``"auto_approve"``, or ``""`` (passthrough). Built here so
    no channel package needs to import ``kiro_crew.slack``.

    The gate also carries ``last_deny_reason``: the hook's own reason for the
    most recent ``"deny"`` and ``""`` otherwise, set on every call. The
    ``TurnDriver`` reads it after a deny and steers it into the running turn
    before the reject, so the model learns which rule blocked the call instead
    of reading kiro-cli's generic "User denied tool execution" -- the same
    attribute-on-a-callable shape ``ApprovalDecider.last_deny_cause`` uses. A
    plain callable without the attribute is a deny with no reason, as before.
    """

    def _tool_gate(event: Any) -> str:
        result = ctx_builder.hooks.on_tool_call(
            getattr(event, "title", "") or "",
            session_key=session_key,
            agent=agent,
            **hook_gate_kwargs(event),
        )
        denied = result.action == TOOL_DENY
        reason = str(getattr(result, "reason", "") or "") if denied else ""
        _tool_gate.last_deny_reason = reason  # type: ignore[attr-defined]
        if denied:
            return "deny"
        if result.action == TOOL_AUTO_APPROVE:
            return "auto_approve"
        return ""

    _tool_gate.last_deny_reason = ""  # type: ignore[attr-defined]
    return _tool_gate


def build_auto_approve(ctx_builder: Any) -> Callable[[Any], bool]:
    """Preserve the ``auto_approve_subagent_spawn`` hook for ``spawn_run``.

    The predicate takes the PERMISSION EVENT, not the title: the title is
    model-authored, so the spawn check keys on ``event_is_spawn_run``'s
    canonical identity (``tool_name`` from ``_meta.kiro``; without it the
    rung does not fire and the request falls to the approval ladder).
    """

    def _auto_approve(event: Any) -> bool:
        return bool(
            ctx_builder
            and ctx_builder.hooks
            and ctx_builder.hooks.auto_approve_subagent_spawn
            and event_is_spawn_run(event)
        )

    return _auto_approve


def _toolless_turn_work_dir(session_key: str) -> Any:
    """The isolated cwd a ``deny_all_tools`` turn cold-starts in.

    The per-session work directory under the workspace root: created on first
    use, owned by this session key alone, and never a project checkout, so no
    ``.kiro/agents`` entry there can shadow the tool-less spec.
    """
    from kiro_crew.config.loader import _session_work_dir

    path = _session_work_dir(session_key)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _provider_backend(provider: Any) -> str | None:
    """The ACP backend id a provider drives, ``None`` when it cannot be read.

    Read through ``provider.client.backend`` with ``getattr`` at both hops, the
    same mock-safe shape ``session._is_claude_backend`` uses. ``None`` (not
    ``""``) is the unreadable answer: the empty string IS ``ACP_BACKEND_KIRO``,
    so collapsing an absent client onto it would route an unknown harness as the
    one that honours the spec. The caller treats ``None`` as refused.
    """
    client = getattr(provider, "client", None)
    if client is None:
        return None
    backend = getattr(client, "backend", None)
    return backend if isinstance(backend, str) else None


def _toolless_spec_before_spawn(session_key: str) -> bool:
    """The pre-spawn read of :data:`TOOLLESS_TURN_AGENT`'s spec, in the turn's own work directory.

    Taken before ``get_or_create`` can cold-start the harness, so
    :func:`_refuse_unless_toolless` can require the spec to mount nothing both
    before the spawn and after it. Resolved project level first, then user
    level, in the same isolated directory the turn's cold start uses.

    Blocking (creates the directory, reads the spec); callers run it with
    ``asyncio.to_thread``. Fails closed.
    """
    from kiro_crew.agent_sdk.agent_specs import authored_spec_is_toolless

    try:
        work_dir = _toolless_turn_work_dir(session_key)
    except Exception:
        logger.warning("tool-less turn work directory unavailable", exc_info=True)
        return False
    return authored_spec_is_toolless(TOOLLESS_TURN_AGENT, work_dir)


def _refuse_before_spawn_unless_toolless(session_key: str, authored_before_spawn: bool) -> None:
    """Raise :class:`ToollessTurnUnavailable` before any session starts when the pre-spawn read failed.

    Starting the session would launch whatever the spec names (an MCP server runs
    its startup code), and a reset afterwards cannot undo that, so a spec the
    pre-spawn read already refused is refused here, with its SEL row, before
    ``get_or_create``.
    """
    if authored_before_spawn:
        return
    sel().log_api_access(
        caller=session_key,
        operation="turn_agent",
        outcome="denied",
        source="messaging",
        resources=f"deny_all_tools turn: {TOOLLESS_TURN_AGENT} spec is not tool-less before spawn",
    )
    raise ToollessTurnUnavailable(
        f"deny_all_tools turn refused before spawn: the {TOOLLESS_TURN_AGENT} spec mounts "
        "a tool, a grant, an MCP server or a hook"
    )


async def _refuse_unless_toolless(
    sessions: Any, session_key: str, provider: Any, *, authored_before_spawn: bool
) -> None:
    """Raise :class:`ToollessTurnUnavailable` unless this turn can be tool-less.

    A ``deny_all_tools`` turn runs on :data:`TOOLLESS_TURN_AGENT`, and that holds
    only on a backend that mounts what the spec names, for a session bound to that
    agent, whose spec mounts nothing. The last check reads the spec the harness
    loaded (``effective_spec_declares_zero_tools``), with *authored_before_spawn*
    the read :func:`_toolless_spec_before_spawn` took before ``get_or_create``.
    Another program can rewrite a file under the agents directory at any time,
    and kiro-cli runs a tool listed in ``allowedTools`` with no permission
    request for the driver to refuse, so the spec on disk is checked on every
    turn, warm or cold. Each refusal writes its SEL row first. A spec refusal
    also resets the session, so the process that loaded that spec is not reused.
    """
    # The tool-less agent is a SPEC, and only a backend that mounts
    # what the spec names honours it. On any other routing the
    # harness keeps its own native tools, and one a project has
    # pre-approved runs with no permission request for the driver to
    # refuse -- so the turn is refused instead. Positive identity:
    # the routing that holds, never the absence of another harness.
    backend = _provider_backend(provider)
    if backend is None or not toolless_turns_supported(backend):
        sel().log_api_access(
            caller=session_key,
            operation="turn_agent",
            outcome="denied",
            source="messaging",
            resources=(
                f"deny_all_tools turn on backend={'unknown' if backend is None else backend!r}: "
                "the tool-less agent spec is not honoured there"
            ),
        )
        raise ToollessTurnUnavailable(
            "deny_all_tools turn refused: this backend does not mount tools "
            "from the agent spec, so an untrusted sender's turn cannot be "
            "made tool-less on it"
        )
    # ``get_or_create`` ignores ``agent`` for a session that already
    # exists, so a key shared with a tooled session would silently run
    # this turn with the operator's tools. Read the binding back and
    # refuse rather than trust the key's shape.
    bound = sessions.get_agent(session_key) if hasattr(sessions, "get_agent") else ""
    if bound and bound != TOOLLESS_TURN_AGENT:
        sel().log_api_access(
            caller=session_key,
            operation="turn_agent",
            outcome="denied",
            source="messaging",
            resources=f"deny_all_tools turn on a session bound to agent={bound!r}",
        )
        raise ToollessTurnUnavailable(
            "deny_all_tools turn refused: its session is bound to an agent "
            "with tools; channels must key untrusted turns separately"
        )
    client = getattr(provider, "client", None)
    confirm = getattr(client, "effective_spec_declares_zero_tools", None)
    confirmed = False
    if callable(confirm):
        try:
            confirmed = (
                await asyncio.to_thread(confirm, authored_before_spawn=authored_before_spawn)
            ) is True
        except Exception:
            logger.warning("tool-less spec confirmation failed", exc_info=True)
    if not confirmed:
        sel().log_api_access(
            caller=session_key,
            operation="turn_agent",
            outcome="denied",
            source="messaging",
            resources=f"deny_all_tools turn: {TOOLLESS_TURN_AGENT} spec is not tool-less",
        )
        try:
            await sessions.reset(session_key)
        except Exception:
            logger.warning(
                "could not reset session %s after a tool-less spec refusal",
                session_key,
                exc_info=True,
            )
        raise ToollessTurnUnavailable(
            f"deny_all_tools turn refused: the {TOOLLESS_TURN_AGENT} spec the harness "
            "loaded mounts a tool, a grant, an MCP server or a hook"
        )


def _without_deny_reason(gate: Callable[[Any], str]) -> Callable[[Any], str]:
    """*gate* as a plain callable, so the driver finds no ``last_deny_reason`` on it."""

    def _plain_gate(event: Any) -> str:
        return gate(event)

    return _plain_gate


# Named in tracebacks, log lines and reprs, so they keep the module callers import them from.
ToollessTurnUnavailable.__module__ = "kiro_crew.messaging.dispatch"
