"""LLMProvider ABC and provider-agnostic event types.

All LLM backends (ACP, Bedrock) implement LLMProvider.  Consumers
(handler, gateway, CLI) depend only on this interface, never on a
concrete provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from functools import cached_property
from typing import TYPE_CHECKING, AsyncContextManager, Literal, Protocol, runtime_checkable

# Event kinds — re-exported from the single source of truth
from kiro_crew.acp.types import (  # noqa: F401
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_NOTICE,
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
from kiro_crew.essential_delivery import EssentialDelivery

# The abort address a provider mints. Safe at module scope despite this module's
# import-light rule: ``abort`` imports only ``mcp_gateway.transport`` outside the
# standard library, and that module is already loaded by the time this one is.
from kiro_crew.mcp_gateway.abort import RuntimeAbortTarget
from kiro_crew.start_priority import StartPriority

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-only: this module's runtime imports are deliberately narrow, and
    # recovery.ladder pulls in metrics.
    from pathlib import Path

    from kiro_crew.agent_sdk.tool_search import ToolSearchSettings
    from kiro_crew.recovery.ladder import InfraError

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

    #: One line naming the servers this session cannot use, empty when it can use
    #: them all. Declared beside ``payload`` because a consumer outside the ACP
    #: layer cannot reach the renderer any other way: the agent-SDK boundary gate
    #: refuses it a new ACP import, so the capability has to be nameable HERE or
    #: it is reachable only by probing an attribute.
    def problem_summary(self, *, include_reasons: bool = True) -> str: ...

    def record_event(
        self, kind: str, server_name: str, error: str = "", *, fanout_no_owner: bool = False
    ) -> bool: ...


class LLMProvider(ABC):
    """Abstract LLM backend."""

    #: The priority of this provider's next ``start()``, set by ``SessionManager``
    #: just before it starts the provider (rule: ``kiro_crew.start_priority``); a
    #: provider started anywhere else starts BACKGROUND.
    start_priority: StartPriority = StartPriority.BACKGROUND

    @cached_property
    def essential_delivery(self) -> EssentialDelivery:
        """Private prompt receipts belong to this provider, never the builder."""
        return EssentialDelivery()

    @property
    def context_incarnation(self) -> object:
        """Identity of the native conversation retaining injected instructions."""
        return (id(self), self.session_id)

    @property
    def context_provider_type(self) -> str:
        """Actual provider label used by context assembly, not global config."""
        return "acp"

    @property
    def native_steering(self) -> bool:
        """Whether the serving harness owns conditional steering selection."""
        return False

    @property
    def native_context_documents(self) -> dict[str, str]:
        """Exact documents supplied at native startup, empty without evidence."""
        return {}

    @property
    def member_dispatch_mounted(self) -> bool | None:
        """Whether this session holds the member session-control tools.

        ``None`` means no evidence: the context builder then falls back to the
        configured member backend's capability. A provider that composes a
        session's MCP array answers True/False from that composition.
        """
        return None

    @abstractmethod
    async def start(self) -> None:
        """Initialize the provider (spawn process, create client, etc.)."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Gracefully shut down."""

    @abstractmethod
    async def stream(self, message: str, *, allow_image: bool = True) -> AsyncIterator[LLMEvent]:
        """Send a message and yield events.

        ``allow_image=False`` sends *message* as text only: no image path in it
        is read or inlined as an image block.
        """
        yield LLMEvent(kind=EVENT_COMPLETE)  # pragma: no cover

    @abstractmethod
    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> bool:
        """Approve a pending tool permission request and report whether it was sent.

        Return ``True`` after sending an allow answer. Return ``False`` when the
        transport rejected the request instead.

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
    def defer_replay_sid_promotion(self) -> bool:
        """Whether replay settlement must precede publishing a fresh native SID.

        The safe default is False: adapters added later publish their own session
        identity normally unless they explicitly adopt the deferred-SID contract.
        """
        return False

    @property
    def is_kiro_backend(self) -> bool:
        """True only when the provider positively identifies as kiro-cli.

        The safe default is False: adapters added later cannot accidentally earn
        Kiro-only behavior merely by omitting this capability.
        """
        return False

    @property
    def is_claude_backend(self) -> bool:
        """True when this provider drives claude-agent-acp."""
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

    @property
    def last_compaction_transient(self) -> bool:
        """Whether that failure is worth retrying.

        The default is the SAFE value: False means "treat it as permanent", so a
        provider that reports no verdict gives up the turn exactly as it did
        before this capability existed, rather than replaying a message against
        a compaction that cannot succeed.
        """
        return False

    @property
    def last_infra_error(self) -> InfraError | None:
        """The L1 verdict on the LAST tool result, or None for "no verdict".

        Declared here so the session layer never has to probe an adapter for the
        attribute: the default None is the SAFE value, the same rule as
        ``last_compaction_transient``. A provider that cannot classify tool
        results gives up the turn exactly as it did before L1 existed.

        Read-only on purpose — the classifying layer (``AcpSessionHandle``) is
        the sole writer, so no caller can fabricate a verdict to force a tool
        re-issue.
        """
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
        """Return True if the provider's backing runtime/connection is alive.

        RUNTIME-level, not session-level: a runtime is one process, and it may
        serve several sessions at once. So this answers "is the process behind me
        standing", which a co-tenant's activity can keep true after this
        session's own work is over. A caller that needs to know whether THIS
        session may still be served asks the registry for its lease, not this.
        """
        return True

    def is_process_alive(self) -> bool:
        """Runtime liveness read off the OS, skipping activity-staleness heuristics.

        Defaults to ``is_alive``. Providers backed by a child process (ACP)
        override this to inspect the OS-level state directly.

        Same scope caveat as ``is_alive``, and sharper here because the reading is
        the operating system's: the answer describes one process, so on a shared
        runtime every tenant gets the same True and no tenant learns anything
        about its own standing from it.
        """
        return self.is_alive()

    @property
    def process_instance(self) -> str:
        """Identity of the CURRENT runtime process serving this provider.

        ``""`` for providers not backed by a child process, and for a
        process-backed provider whose process is gone. Process-backed providers
        override this with a per-spawn token so a resource minted inside one
        process (an MCP OAuth authorize link, whose loopback listener and PKCE
        verifier live there) can be recognized as dead once the process is —
        equality across spawns must be impossible, which is why the ACP session
        id (reused by resume on a new process) can never serve here.

        A process fact, so every session on a shared runtime reads the SAME
        token. That is what makes it sound for "is this still the process I
        observed" and unsound for "is this my session": it answers the first
        question and must never be folded into a session identity.
        """
        return ""

    @property
    def member_capabilities_supported(self) -> bool:
        """Whether a dedicated startup can load a complete member agent spec."""
        return False

    @property
    def loaded_capability_template(self) -> str:
        """Confirmed active full-spec template; empty means no loading evidence."""
        return ""

    @property
    def exit_code(self) -> int | None:
        """The runtime process's last exit code; None with no process or still running.

        A runtime fact: the code describes how one process ended, which is the
        same answer for every session that was being served on it. So it explains
        a runtime's death, never why one particular session stopped — a session
        that merely lost its lease has no exit code of its own.
        """
        return None

    @property
    def cwd(self) -> str:
        """Working directory the provider operates in. Default: empty string."""
        return ""

    def set_work_dir_claim_probe(
        self,
        probe: Callable[[], AsyncContextManager[bool]],
    ) -> None:
        """Install the registry claim held across a work-directory reclaim.

        The yielded answer is evaluated while the session registry lock remains
        held through the reclaim operation, so a successor cannot register
        between the ownership decision and deletion. Default no-op for a
        provider that never reclaims a directory.
        """
        return None

    def disown_work_dir(self) -> None:
        """Declare that this provider does not own its work directory for reclaim.

        The session registry calls this before shutting down a provider whose
        session KEY another live provider already holds: both derived the same
        work directory from that key, so the one being discarded must not
        remove it at shutdown (``session_work_dir``). Default no-op for a
        provider that never reclaims a directory.
        """
        return None

    @property
    def served_model(self) -> str:
        """Model id the live session actually resolved to serve.

        Public accessor so callers (e.g. the poisoned-conversation canary in
        chat_runner) never reach through provider internals. Default: empty
        string, meaning "unknown" — callers must treat that as inconclusive,
        never as a wildcard.
        """
        return ""

    @property
    def model_pin_refused(self) -> str:
        """The pinned model the backend refused at session start, or ``""``.

        A refusal on a non-strict model push leaves the session on the backend
        default without raising, so this is how a caller billing or labelling a
        turn by the pin learns the pin never ran. Default: ``""``, no refusal.
        """
        return ""

    @property
    def model_pin_partial(self) -> str:
        """The bare model a ``<model>[<effort>]`` pin landed as, or ``""``.

        Set when the model half of a pair pin applied and the effort half did
        not: the session runs this bare model, not the pin and not the default.
        Default: ``""``, no partial application.
        """
        return ""

    def touch_activity(self) -> None:
        """Refresh the RUNTIME's activity timestamp without I/O. Default no-op.

        The stamp lives on the process, so on a shared runtime one session
        touching it marks every session on that process active. Idle-expiry
        therefore reads a runtime's idleness, not a session's, and a surface that
        needs the latter keeps its own clock.
        """
        return None

    def runtime_abort_target(self) -> RuntimeAbortTarget | None:
        """This session's runtime as an opaque abort address, or ``None``.

        Handed to ``mcp_gateway.abort.schedule_abort_for`` unopened. The target is
        deliberately opaque because a runtime is one process serving possibly
        several sessions: a session that could read its pid here could also
        attribute that process to itself, and the abort seam is the one place the
        pid legitimately reaches a wire.

        ``None`` means no runtime of this provider's own is reachable, and the
        abort push is skipped. The default walks the private process fields a
        provider outside this tree may expose (``_client._pid`` with
        ``_client._mcp_gateway_socket``) so such a provider keeps its abort push
        without declaring anything; a provider in this tree overrides instead.

        A provider whose runtime is reached some OTHER way overrides this. The
        default only knows the ``_client`` shape, so an override is how a provider
        with a different one keeps its abort push.

        Scope note carried from the wire: the frame names a PROCESS, so aborting
        it cancels in-flight tool calls for every session on that process. At a
        runtime cap of 1 the process has one tenant and the two scopes coincide.
        """
        client = getattr(self, "_client", None)
        if client is None:
            return None
        return RuntimeAbortTarget.build(
            getattr(client, "_pid", None),
            getattr(client, "_mcp_gateway_socket", None),
        )

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

    def background_launch(self) -> tuple[float, str] | None:
        """``(seconds since, description)`` of the newest work this session's
        harness launched to run on after the prompt returned (a backgrounded
        command, a Workflow), or ``None``. Read by the session watchdog, which
        cannot otherwise see that work. Default ``None``: nothing launched."""
        return None

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
    def supports_refusal_steer(self) -> bool:
        """True when a deny notice steered mid-turn reaches the refused turn's model.

        Narrower than :attr:`supports_steer`: a harness can take a user's mid-turn
        message and still discard one sent while a refused tool call is being
        answered. Default False, granted by opt-in like the steer itself.
        """
        return False

    @property
    def steer_needs_loss_recovery(self) -> bool:
        """True when a steer this provider accepted can still be dropped.

        codex drops injected text when a later approval in the turn is denied or
        the turn is cancelled, so only a caller that keeps and requeues the text
        (the dashboard composer) may steer it; other callers queue instead of
        steering such a provider. Default False.
        """
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
    def kas_auto_approved_capabilities(self) -> frozenset[str] | None:
        """The capabilities this session's registered agent batch auto-approves.

        ``None`` means the session registered no batch: a host that took its agent
        at spawn time, or a provider that has not started a session yet. Only a
        wire-registered host (KAS) answers with a set; see
        :func:`kiro_crew.agent_sdk.spec_hooks.hook_projection_stale`, whose answer
        for ``None`` is "not stale", because such a session has nothing a later
        hook could have been left out of.
        """
        return None

    @property
    def kas_projected_agent(self) -> str:
        """The agent this session's registered agent batch was built for.

        ``""`` when no batch was registered. A turn that names no agent of its own
        runs this one, so this is whose spec hooks it meets (see
        :func:`kiro_crew.agent_sdk.spec_hooks.turn_spec_hooks`).
        """
        return ""

    @property
    def tool_search_settings(self) -> "ToolSearchSettings | None":
        """The operator's MCP Tool Search choice this provider spawned with, or
        ``None`` when it carries none.

        Read by whoever builds a runtime on this provider's behalf (a companion
        runtime for a sub-agent) so that runtime is handed the SAME setting the
        parent's handshake sent, rather than being left to the host's default.
        Declared here with a safe default rather than probed off the instance
        (harness-parity H14): a provider that never threaded the setting answers
        ``None`` and the runtime it seeds stays exactly as before. The ACP
        provider answers with its resolved ``ToolSearchSettings``.
        """
        return None

    @property
    def work_scratch_dir(self) -> "Path | None":
        """The ``$KIROCREW_SCRATCH`` directory the RUNTIME serving this session
        exposes, or ``None`` when it has none.

        A runtime fact: the directory was allocated for a process, so every
        session sharing that process answers with the same path and the tree they
        form has one work directory between them. That is the intent — a spawned
        child joins the directory it was given — and it is also why the path is
        not a place to keep anything scoped to a single session.

        Read by whoever spawns a process on this session's behalf (a companion
        runtime, a dedicated sub-agent process) so that process mounts the SAME
        directory and the session tree keeps one work directory
        (``agent_scratch``). Declared here with a safe default rather than probed
        off the instance (harness-parity H14): a provider that never allocated
        scratch answers ``None`` and the child starts its own directory, exactly
        as before the capability existed. The ACP providers answer the directory
        their live process was spawned with.
        """
        return None

    @property
    def manual_compact_unsupported_backend(self) -> str | None:
        """Backend id when this provider cannot serve a manual ``/compact``,
        ``None`` when the command is fine to dispatch.

        The manual entry points gate on this so an unsupported backend gets an
        immediate, user-visible refusal instead of a prompt whose
        compaction-status wait strands until ``COMPACT_WAIT_TIMEOUT_SECS``.
        Default ``None`` — a provider that has not positively named an
        unsupported backend passes through, because it handles ``/compact`` on
        its own terms. Declared here with a safe default rather than probed off
        the instance (harness-parity H14); the ACP implementations answer from
        ``ACP_BACKENDS_COMPACT`` membership. Consumers must act only on a
        non-empty ``str`` value, so a mocked provider's attribute never reads
        as a refusal."""
        return None

    @property
    def compaction_self_managed(self) -> bool:
        """Whether the harness itself bounds this session's context.

        The third compaction question, and the one that separates the two
        declines. ``manual_compact_unsupported_backend`` says Crew may not send
        ``/compact``; ``compaction_unmanaged_backend`` says Crew will recycle
        instead. A backend that answers the first and not the second is EITHER a
        harness that summarizes unasked, OR a harness nobody has classified yet —
        indistinguishable from those two answers alone, and they deserve
        different words and different log levels.

        Default ``True`` — a provider that has not spoken is taken to manage its
        own context, which is the reading that changes no message and no level.
        Declared here rather than probed off the instance (harness-parity H14);
        the ACP implementations answer from
        ``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION`` membership."""
        return True

    @property
    def compaction_unmanaged_backend(self) -> str | None:
        """Backend id when NOTHING compacts this session, ``None`` otherwise.

        The strictly narrower half of
        :attr:`manual_compact_unsupported_backend`, and the two answer different
        questions. That one asks whether CREW may send a ``/compact`` prompt. This
        one asks what happens when Crew may not: a harness that summarizes on its
        own initiative and reports it on the wire needs nothing from Crew, while a
        harness that reports nothing needs the session recycled or its context grows
        until the window ends the conversation for it.

        Default ``None`` — a provider that has not positively named such a backend
        is left alone, because this is the one answer that ENDS a conversation and
        it must never be reached by default. Declared here with a safe default
        rather than probed off the instance (harness-parity H14); the ACP
        implementations answer from ``ACP_BACKENDS_CONTEXT_RECYCLE`` membership,
        which names its members rather than taking whatever the other two sets
        leave over. Consumers must act only on a non-empty ``str`` value, so a
        mocked provider's attribute never reads as a claim that nothing
        compacts."""
        return None

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

    @property
    def mcp_config_hot_reload(self) -> bool:
        """True when this provider's live process reconciles agent-config and
        ``mcp.json`` edits on its own — only the changed MCP servers restart, the
        conversation is kept — so the dashboard's MCP sync may leave it running
        instead of resetting it.

        Default False — the reset is the safe answer, and a harness that has not
        demonstrated the reconcile must not inherit the skip (harness-parity
        H6/H14). Declared here rather than probed off the instance; the ACP
        implementation answers from ``ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD``
        membership plus the version its process reported at ``initialize``.
        Consumers must act only on a literal ``True``, so a mocked provider's
        attribute never reads as a skip."""
        return False

    def available_models(self) -> list[dict[str, str]]:
        """Backend-advertised models (``[{modelId, name, ...}]``) for the model
        picker. Default empty for a provider that advertises none."""
        return []

    async def maybe_refresh_available_models(self, catalog_ids: list[str]) -> list[dict[str, str]]:
        """Revalidate the advertised-model snapshot before the picker narrows with it.

        The model list (`/api/models`) narrows the catalog through the newest live
        session's snapshot. When that snapshot is a startup-race default it hides
        models the account actually has, and no explicit pick is refused to
        trigger the refusal-path heal, so the read path must ask to revalidate.

        Declared HERE rather than probed with ``getattr`` at the consumer: a probe
        answers "cannot revalidate" for a provider that simply spells the accessor
        differently, which is indistinguishable from a provider that genuinely has
        no probe — and the consumer would then silently narrow on a stale snapshot,
        the exact failure this revalidation exists to remove. A provider with no
        way to revalidate returns its current snapshot unchanged (fail open), which
        this default does; ``catalog_ids`` is the unfiltered catalog the picker
        would otherwise offer, and the keep/drop verdict stays with the caller.
        """
        return self.available_models()

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

    def supports_effort(self) -> bool:
        """True when the current model accepts a reasoning-effort level. Default False."""
        return False

    async def change_effort(self, level: str) -> bool:
        """Change reasoning effort live for the current model. Returns True on success,
        False when effort is unsupported. Default False."""
        return False

    async def clear_effort(self) -> bool | None:
        """Clear the slot's reasoning-effort override for the current model.

        True applied a default LIVE, False needs a session reset to reach it, and
        None means NOTHING changed -- neither the workspace overlay nor the
        provider's own map -- so the caller must commit no new slot value and
        reset nothing. Default False.
        """
        return False
