"""LLMProvider ABC and provider-agnostic event types.

All LLM backends (ACP, Bedrock) implement LLMProvider.  Consumers
(handler, gateway, CLI) depend only on this interface, never on a
concrete provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Literal, Protocol, runtime_checkable

# Event kinds — re-exported from the single source of truth
from kiro_crew.acp.types import (  # noqa: F401
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TODO_UPDATE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
)
from kiro_crew.acp.types import AcpEvent as LLMEvent  # noqa: F401
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS

CancelOutcome = Literal["acked", "timeout", "no_turn", "error"]


def resolve_billing_stats(holder: object | None) -> object | None:
    """The billing stats *holder* declares, else the ``last_prompt_stats`` it carries.

    The single spelling of "read a turn's billing off this object", shared by the
    accounting path and by every wrapper that forwards it, so a wrapper cannot
    resolve the capability differently from the reader it feeds.

    The declaration is resolved off the TYPE, not the instance: a class that
    defines :meth:`LLMProvider.billing_stats` is stating the capability, whereas
    an object that merely answers an attribute of that name may be a mock whose
    auto-created child would masquerade as a stats object and shadow the real
    billing. A holder that declares nothing -- or leaves the ABC default, which
    answers ``None`` -- falls back to the ``last_prompt_stats`` the ACP runner
    carries, so a holder found before the capability existed is still read.
    """
    if holder is None:
        return None
    seam = getattr(type(holder), "billing_stats", None)
    if callable(seam):
        declared = seam(holder)
        if declared is not None:
            return declared
    return getattr(holder, "last_prompt_stats", None)


@runtime_checkable
class SessionMcpReport(Protocol):
    """What a session's MCP registration report offers its consumers.

    Declared at the provider seam rather than imported from ``kiro_crew.acp`` so
    a dashboard consumer can name the capability without taking an ACP-layer
    edge. The concrete ``McpSessionReport`` satisfies it structurally.
    """

    def payload(self) -> dict | None: ...

    def record_event(
        self, kind: str, server_name: str, error: str = "", *, fanout_no_owner: bool = False
    ) -> bool: ...


class LLMProvider(ABC):
    """Abstract LLM backend."""

    @abstractmethod
    async def start(self) -> None:
        """Initialize the provider (spawn process, create client, etc.)."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Gracefully shut down."""

    @abstractmethod
    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        """Send a message and yield events."""
        yield LLMEvent(kind=EVENT_COMPLETE)  # pragma: no cover

    @abstractmethod
    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        """Approve a pending tool permission request.

        ``always=True`` signals the user picked the "always allow" option
        (e.g. trust mode). Providers that distinguish between one-shot and
        persistent approval (ACP backends echoing optionId, claude-agent-acp
        emitting an addRules suggestion) should honor it; others may treat
        it as a synonym for one-shot allow.
        """

    @abstractmethod
    async def reject_tool(self, request_id: str | int) -> None:
        """Reject a pending tool permission request."""

    @abstractmethod
    def context_usage_pct(self) -> float:
        """Return last known context usage percentage."""

    def context_usage_unknown(self) -> bool:
        """True when a 0% reading means "unknown", not "empty".

        A backend that compacts in place reports 0% for a transcript whose real
        size it has not measured yet, which is byte-identical to a brand-new
        session. Callers that act on a threshold need the two apart. Default
        False for providers that never compact unobserved.
        """
        return False

    @property
    def child_fidelity_aware(self) -> bool:
        """Consumer opt-in for the low-fidelity CHILD permission downgrade.

        Backend-internal subagents (children spawned inside the backend
        process) can escalate permission requests whose structured security
        context is absent. A consumer that implements the downgrade (e.g.
        an interactive card) sets this True so those events are delivered
        to it; while False, the session layer fail-closes them (reject)
        so no consumer can auto-approve a child on agent-authored context.

        Declared on the ABC so every conforming adapter has the attribute:
        the default is the SAFE value (False → fail-closed), and providers
        without child sessions can ignore it entirely. Runtime-backed
        providers override with a real forwarding property.
        """
        return False

    @child_fidelity_aware.setter
    def child_fidelity_aware(self, value: bool) -> None:
        """Accept and discard by default — providers without child sessions
        have nothing to forward to, and the False getter above remains the
        (safe) truth for them."""
        return None

    def context_window_tokens(self) -> int:
        """Return the real served context window in tokens (0 if unknown).

        Used by the dashboard to render accurate "used / window" token text
        instead of re-deriving the window from the model id. Default 0 so
        providers that don't report a window simply omit the token text.
        """
        return 0

    def context_used_tokens(self) -> int:
        """Return the tokens used in the current context (0 if unknown).

        Pairs with ``context_window_tokens`` for the dashboard's absolute
        "used / window" token text. Default 0.
        """
        return 0

    @property
    def session_id(self) -> str:
        """Provider-specific session identifier for file cleanup.

        Returns empty string if the provider has no persistent session files.
        Each provider overrides to return its own session_id.
        """
        return ""

    async def cleanup_session(self, session_id: str) -> None:
        """Delete on-disk session files for the given session ID.

        Default implementation is a no-op. Providers with persistent
        session files override this to perform actual deletion.

        cleanup_session only operates on the filesystem (Path.unlink,
        shutil.rmtree). It does NOT depend on the provider process being
        alive. This makes fire-and-forget via asyncio.ensure_future safe —
        the cleanup task only needs the session_id string, not a live process.
        """

    async def stream_command(self, command: str) -> AsyncIterator[LLMEvent]:
        """Execute a slash command and yield streaming events.

        Default falls back to :meth:`stream` for providers without native
        command support.
        """
        async for event in self.stream(command):
            yield event

    async def compact(self, context: str = "") -> None:
        """Trigger context compaction. No-op for providers without native support."""

    async def wait_for_compaction(self, timeout: float = COMPACT_WAIT_TIMEOUT_SECS) -> dict:
        """Wait for compaction completed/failed. Returns ``{'type': 'timeout'}`` by default."""
        return {"type": "timeout"}

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        """Cancel in-flight operation. Returns CancelOutcome."""
        return "no_turn"

    def is_alive(self) -> bool:
        """Return True if the provider's backing process/connection is alive."""
        return True

    def is_process_alive(self) -> bool:
        """Process-level liveness check (skips activity-staleness heuristics).

        Defaults to ``is_alive``. Providers backed by a child process (ACP)
        override this to inspect the OS-level state directly.
        """
        return self.is_alive()

    @property
    def exit_code(self) -> int | None:
        """Last child-process exit code, or None if no process or still running."""
        return None

    @property
    def cwd(self) -> str:
        """Working directory the provider operates in. Default: empty string."""
        return ""

    @property
    def served_model(self) -> str:
        """Model id the live session actually resolved to serve.

        Public accessor so callers (e.g. the poisoned-conversation canary in
        chat_runner) never reach through provider internals. Default: empty
        string, meaning "unknown" — callers must treat that as inconclusive,
        never as a wildcard.
        """
        return ""

    def touch_activity(self) -> None:
        """Refresh provider activity timestamp without I/O. Default no-op."""
        return None

    def runtime_info(self) -> tuple[int | None, str | None]:
        """Return (runtime_pid, gateway_socket_path) for abort propagation.

        Subclasses that manage a child process override this to return the
        real values. Base returns (None, None) which disables abort push.
        """
        return (None, None)

    def billing_stats(self) -> object | None:
        """The live per-turn billing stats object, or None when unmetered.

        Declared here so what a turn COST is a stated provider capability, like
        the context accessors above, instead of an attribute name the accounting
        path has to guess. Surfaces that dispatch without an ``EVENT_COMPLETE``
        in hand -- cron, heartbeat, autonudge, workflows, the task runner --
        recover the turn's spend through ``llm_helpers.provider_last_turn_usage``,
        which otherwise finds it only by walking the private attributes
        ``_client`` / ``_handle`` / ``_sess`` / ``provider`` for a
        ``last_prompt_stats``. A provider that links to its turn-runner under any
        other name is not found by that walk: the read yields empty usage, the
        ``usage_has_billing`` gate reads that as "nothing to record", and the
        spend never reaches the usage store -- absent from the dashboard while
        the account balance moves, with no error raised anywhere.

        Return the stats OBJECT, not a value. The accounting path compares
        identity to tell a turn that ran from one whose dispatch failed while a
        previous, already-recorded turn's stats were still installed, so a
        provider must install a fresh object as each turn begins (as the ACP
        runner does) for that comparison to hold.

        The object need only carry the billing: ``to_turn_usage()`` is preferred
        when it offers one -- that is the single source of truth for every
        dimension a seam bills on -- and a ``credits`` attribute is read
        otherwise. Default None means "this backend reports no per-turn
        billing", so an unmetered provider needs no override.
        """
        return None

    # ── Turn-control and capability surface (harness-parity H14) ──
    # The session, shutdown-drain, steer, and dashboard layers read these off a
    # provider. Declaring them here with a safe default means a provider that
    # lacks the capability (a non-ACP backend, a warm-pool stub) returns the
    # default instead of forcing a ``getattr`` probe onto the Kiro path or
    # AttributeError-ing. Concrete providers override; the caller-side
    # ``getattr``/``hasattr`` guards remain where they additionally defend
    # against test doubles (AsyncMock) that are not LLMProvider instances.

    def has_active_turn(self) -> bool:
        """True if a prompt is in flight and not yet cancelled. Default False."""
        return False

    def has_unfinished_turn(self) -> bool:
        """True if a native turn has not reached its done boundary, independent
        of cancel state (drives the shutdown drain). Default False."""
        return False

    async def wait_turn_done(self, timeout: float) -> str:
        """Wait for the current native turn's done boundary and return its stop
        reason. Default: no turn to wait for — return immediately with ``""``."""
        return ""

    async def steer(self, message: str) -> bool:
        """Inject a mid-turn steer; return True if accepted. Default False for a
        provider with no steer extension (granted by opt-in, never inherited)."""
        return False

    @property
    def supports_steer(self) -> bool:
        """True when the provider implements mid-turn steer. Default False."""
        return False

    @property
    def last_steer_monotonic(self) -> float:
        """Monotonic time of the last steer this provider handed to its backend,
        0.0 when it has never steered one.

        Part of the steer capability rather than an optional extra to it. The
        dashboard's keepalive route compares this against the reading taken when
        a sleeping ``wait`` began, because a sleep is one of the few places a
        steer cannot be injected: the backend needs a model-inference boundary
        and an in-flight tool call is the absence of one. So a provider that
        returns True from :attr:`supports_steer` and leaves this at the default
        steers perfectly well and silently never interrupts a wait — a failure
        with no error to notice. ``test_harness_parity`` ratchets the two
        together for that reason; the route's own defensive default is a
        backstop, not the guarantee.
        """
        return 0.0

    @property
    def is_session_sharing_eligible(self) -> bool:
        """True when the provider can host multiplexed sub-agent sessions on one
        process. Default False — session sharing is opt-in, never inherited."""
        return False

    @property
    def uses_kiro_identity_store(self) -> bool:
        """True when this provider's child authenticates from kiro-cli's own
        identity store, so an external ``kiro-cli logout`` invalidates a process
        that is already running and it must be retired.

        Default False — a provider authenticated some other way must not be
        recycled on a store it never reads, and a harness that has not stated
        that it reads that store does not inherit the claim (harness-parity
        H5/H14). Declared here rather than probed off the instance so the session
        layer never has to guess from private attributes."""
        return False

    def available_models(self) -> list[dict[str, str]]:
        """Backend-advertised models (``[{modelId, name, ...}]``) for the model
        picker. Default empty for a provider that advertises none."""
        return []

    def mcp_session_report(self) -> SessionMcpReport | None:
        """This session's own MCP registration report, or None if it keeps none.

        Declared HERE rather than probed with ``getattr`` at the consumer: a probe
        answers "no report" for a provider that simply spells the accessor
        differently, which is indistinguishable from a session that reported
        nothing — and that silence is the false all-clear the report exists to
        remove. A provider without one returns None explicitly.
        """
        return None

    def get_valid_effort_levels(self) -> list[str]:
        """Reasoning-effort levels the provider accepts. Default empty for a
        provider with no effort control."""
        return []
