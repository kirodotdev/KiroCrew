"""The contract between an ``AcpSessionHandle`` and the runtime it rides on.

The ``AcpRuntimeError`` family the handle and ``AcpRuntime`` raise, and ``AcpRuntimeProtocol``,
the interface the handle needs from the runtime. ``kiro_crew.acp.session_handle`` re-exports
every name, and ``kiro_crew.acp.runtime`` imports them from there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle


class AcpRuntimeError(Exception):
    """Base error for AcpRuntime operations."""


class AcpRuntimeDead(AcpRuntimeError):
    """Raised when the underlying process has died.

    ``ambiguous_delivery`` is True when the death followed a request-frame drain
    stall whose bytes had already reached the transport (see
    :class:`AcpProcessDied` for the recovery consequence); it rides through
    ``AcpSessionProvider._translate_dead`` onto the ``AcpProcessDied`` the caller
    recovers from. False for every other death, including a lock-phase stall that
    wrote nothing.
    """

    def __init__(self, *args: object, ambiguous_delivery: bool = False) -> None:
        super().__init__(*args)
        self.ambiguous_delivery = ambiguous_delivery


class AcpFrameTooLarge(AcpRuntimeError):
    """The reply to an awaited request was over the stdout frame limit and dropped.

    Raised in place of the timeout the caller would otherwise hit much later, so
    the error names the real cause -- the frame's size against the limit --
    instead of whatever the request was waiting on (a ``session/new`` timeout
    reads as slow MCP servers). Not ``transient``: the same request gets the same
    reply. ``session_start_failed`` is set by the session-start arms that catch it,
    like :class:`AcpRequestTimeout`'s, so a self-driving caller counts the streak.
    """

    # Read structurally by llm_helpers.acp_error_is_transient, so the verdict never
    # falls back to matching this message's prose.
    transient = False
    session_start_failed = False


class AcpModeNotFound(AcpRuntimeError):
    """kiro-cli answered ``Mode '<mode_id>' not found`` to an awaited request.

    A subclass so every ``except AcpRuntimeError`` keeps catching it, and so the
    one caller that can recover -- a ``session/set_mode`` naming a skill-view
    alias the host has not loaded yet -- can tell it apart structurally rather
    than by matching the user-facing sentence.
    """

    def __init__(self, message: str, mode_id: str) -> None:
        super().__init__(message)
        self.mode_id = mode_id


class AcpAgentUnavailable(AcpRuntimeError):
    """The session's agent has no definition at all, so the session is not created.

    Raised at session start when no spec declares the agent -- typically because
    the app that registered it is disabled -- with a message that names the cause
    and the fix. ``transient = False``: a retry finds the same missing definition.
    ``agent_unavailable`` is the structural tag the chat reads to show the reason
    as a refusal with no Resume, so the verdict never depends on the prose.
    """

    transient = False
    agent_unavailable = True

    def __init__(self, message: str, *, agent: str) -> None:
        super().__init__(message)
        self.agent = agent


class AcpRequestTimeout(AcpRuntimeError):
    """Raised when a request's response does not arrive within its budget.

    Distinguished from a plain ``AcpRuntimeError`` so a caller that knows what
    the request was waiting on can attach that context before it reaches the
    user. Subclasses the base so existing ``except AcpRuntimeError`` handlers
    keep catching it.

    ``transient = True`` is the retry-eligibility verdict read structurally by
    ``llm_helpers.acp_error_is_transient`` (via ``getattr(exc, "transient",
    None)``), the same channel ``AcpError.transient`` uses. Every request that
    can time out here is a control-plane one — ``_send_and_await`` serves only
    ``initialize`` / ``session/new`` / ``session/load`` / ``set_mode`` / teardown,
    never the prompt stream (that path raises ``AcpTimeoutError``). A timeout on
    any of those means the runtime was slow to answer a handshake, not that the
    work failed: no prompt was dispatched, so nothing was attempted to fail. A
    cold-start stall is transient host weather, so the retry layer should try
    again rather than count it. Carrying the verdict on the type — instead of a
    ``"timed out"`` string added to ``_TRANSIENT_MARKERS`` — decides eligibility
    by exception type rather than by message wording, so a reword of the timeout
    message cannot flip retryability.
    """

    transient = True

    # Whether this timeout happened while STARTING a session (``session/new`` /
    # ``session/load`` / ``session/resume``) rather than on any other
    # control-plane request. Carried on the type, and mirrored by
    # ``AcpError.session_start_failed`` on the dedicated-client path, so a
    # self-driving caller can count "my cycle never got a session" without
    # matching on message wording. Read structurally with ``getattr``: the two
    # exception families do not share a base, and only the raise sites that know
    # the method set it True.
    session_start_failed = False


class AcpRuntimeOverloaded(AcpRequestTimeout):
    """``initialize`` went unanswered while the agents slice was being throttled.

    A cold start under the aggregate memory ceiling is slow, not broken: the
    kernel is throttling every process in ``kirocrew-agents.slice``, so a
    healthy kiro-cli can take longer than any fixed handshake budget to answer
    ``initialize``. Raised only when the process was still ALIVE at the
    deadline and the slice was throttling -- a process that exited, or a stall
    on an unthrottled host, stays a plain :class:`AcpRequestTimeout`.

    Subclasses the timeout so every ``except AcpRequestTimeout`` /
    ``except AcpRuntimeError`` handler keeps working. What the subclass
    changes is the retry verdict: ``transient = False``. The parent's
    ``True`` says "host weather, try again", and every retry layer reads it
    structurally (``llm_helpers.acp_error_is_transient``, the taskq
    ``acp_provider`` classifier), so an inherited ``True`` would respawn a
    fresh kiro-cli straight back into the same throttled slice -- each retry
    pays the whole startup again and deepens the throttle it is waiting on.
    Overload is the one timeout where the remedy is NOT to retry: it is to
    reduce concurrent agent memory (close idle sessions, fewer parallel
    subagents), and the message names that remedy so the failure surfaces as
    overload rather than as a crash to debug.
    """

    transient = False


class AcpRuntimeProtocol(Protocol):
    """Minimal interface that AcpSessionHandle needs from AcpRuntime."""

    _last_activity: float
    """Monotonic ts of the last frame the runtime read or wrote, on ANY session.

    Runtime-wide by construction: the single reader bumps it per stdout line, so
    a co-tenant session's traffic keeps it fresh. A caller must NOT read it as
    evidence about one session — neither that this session is progressing nor
    that its turn produced anything — which is why the per-session clocks in the
    dispatch loop carry their own timestamps and only FOLD this one in.

    Read through :meth:`AcpSessionHandle._runtime_idle_secs`, the single seam
    that converts it to an idle duration, so a runtime that later publishes a
    public accessor is adopted in one line instead of at every call site.
    """

    @property
    def pid(self) -> int | None:
        """Subprocess pid (sandbox launcher parent under the Linux namespace
        sandbox) — the liveness oracle scans its descendant tree for evidence."""
        ...

    @property
    def spawn_monotonic(self) -> float | None:
        """Monotonic time the process was spawned, or ``None`` before spawn.

        The read-path entitlement revalidation uses it to tell a snapshot
        captured inside the startup race window (when the degraded free-tier
        answer is resolved) from one captured well after the process settled.
        """
        ...

    @property
    def entitlement_probe_result_at(self) -> float:
        """Monotonic time the stored probe answer arrived (0.0 before any).

        Whether :meth:`probe_advertised_models` served a fresh answer or replayed
        the stored one, the answer is dated by this clock; the handle dates the
        snapshot it stores from here so its own freshness floor never rises above
        the data it holds.
        """
        ...

    @property
    def acp_backend(self) -> str:
        """Which ACP backend the process speaks.

        The handle needs it because the backends disagree on verbs, not just on
        payloads — the model, for one, is a ``session/set_model`` request on
        kiro-cli and a session config option on KAS.
        """
        ...

    @property
    def supports_image_prompt(self) -> bool:
        """Whether the agent advertised ``promptCapabilities.image``.

        Read by :meth:`AcpSessionHandle.prompt` to decide if an image may travel
        as an inline block. Fails closed, so a backend that never handshaked
        gets text only.
        """
        ...

    @property
    def agent_version(self) -> str:
        """``agentInfo.version`` from the handshake — the version the process runs.

        ``""`` until the handshake completes; a capability gate reading it
        fails closed on that.
        """
        ...

    async def send_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        on_reserved: Callable[[int], None] | None = None,
    ) -> int: ...

    async def probe_advertised_models(
        self, *, force: bool = False, not_before: float = 0.0
    ) -> list[dict[str, str]]:
        """Fresh advertised-model snapshot from a throwaway ``session/new``
        (``[]`` = probe failed / advertised nothing — never evidence).

        ``force=True`` skips the failed/empty attempt-clock replay (a user action
        earns a fresh probe); a recent non-empty success is still replayed.
        ``not_before`` is the monotonic capture time of the caller's snapshot: a
        replayed result is served only if it is at least as new as that."""
        ...

    async def send_notification(self, method: str, params: dict[str, Any]) -> None: ...

    async def send_request_for_answer(
        self,
        method: str,
        params: dict[str, Any],
        on_registered: "Callable[[asyncio.Future[dict[str, Any]]], None] | None" = None,
    ) -> "asyncio.Future[dict[str, Any]]": ...

    def forget_request(self, future: "asyncio.Future[dict[str, Any]]") -> None: ...

    async def send_response(
        self,
        request_id: str | int,
        result: dict[str, Any],
        *,
        before_write: Callable[[], None] | None = None,
        withdrawal_check: Callable[[], None] | None = None,
    ) -> None: ...

    async def send_error(self, request_id: str | int, code: int, message: str) -> None: ...

    def mark_turn_active(self, session_id: str, active: bool) -> None: ...

    def begin_mcp_sign_in(self, session_id: str, server_name: str) -> bool: ...

    def mcp_sign_in_holds(self, session_id: str, server_name: str) -> bool: ...

    def unregister_session(self, session_id: str) -> None: ...

    async def terminate_session(self, session_id: str) -> None: ...

    def is_alive(self) -> bool: ...

    @property
    def stdin_stall_death(self) -> bool:
        """Whether the runtime died of a stdin stall with its child still alive."""
        ...

    def turn_active_at_stall(self, session_id: str) -> bool:
        """Whether *session_id* had a turn running when that stall killed the runtime."""
        ...

    def _mark_dead(
        self, reason: str, *, expected: bool = False, stdin_stalled: bool = False
    ) -> None:
        """Fail the runtime: poison every session queue and reject pending waits.

        Declared rather than reached for with ``getattr`` so a runtime that
        cannot honour it fails the type check instead of silently skipping the
        escalation — the handle calls this only when a write it cannot verify
        leaves the child waiting on a stranded oneshot, and a swallowed call
        there is an invisible hang rather than a degraded session.

        A caller must NOT assume it is the only way the runtime dies, that it
        kills the process synchronously, or that a second call does anything:
        it is idempotent, and ``expected=True`` merely marks a death the caller
        caused so the reason does not read as a fault.
        """
        ...

    def death_summary(self) -> str | None: ...

    def _member_projection_needs_confirmation(self, mirror_used: bool) -> bool:
        """Whether this start must confirm the array a mirror built. Synchronous."""
        ...

    async def _confirm_member_projection(self, handle: AcpSessionHandle) -> None:
        """Refuse a member session whose consumed projection is not the saved intent."""
        ...


# Named by the path callers import them from: an error chain renders each raised class
# as ``module.qualname`` (``kiro_crew.subagent._describe_exception``), and that text
# reaches the Subagents panel and tracebacks, so this module's name must not leak into it.
AcpRuntimeError.__module__ = "kiro_crew.acp.session_handle"
AcpRuntimeDead.__module__ = "kiro_crew.acp.session_handle"
AcpFrameTooLarge.__module__ = "kiro_crew.acp.session_handle"
AcpModeNotFound.__module__ = "kiro_crew.acp.session_handle"
AcpAgentUnavailable.__module__ = "kiro_crew.acp.session_handle"
AcpRequestTimeout.__module__ = "kiro_crew.acp.session_handle"
AcpRuntimeOverloaded.__module__ = "kiro_crew.acp.session_handle"
