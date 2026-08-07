"""LLMProvider ABC and provider-agnostic event types.

All LLM backends (ACP, Bedrock) implement LLMProvider.  Consumers
(handler, gateway, CLI) depend only on this interface, never on a
concrete provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Literal

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

CancelOutcome = Literal["acked", "timeout", "no_turn", "error"]


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

    async def wait_for_compaction(self, timeout: float = 120.0) -> dict:
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

    def touch_activity(self) -> None:
        """Refresh provider activity timestamp without I/O. Default no-op."""
        return None

    def runtime_info(self) -> tuple[int | None, str | None]:
        """Return (runtime_pid, gateway_socket_path) for abort propagation.

        Subclasses that manage a child process override this to return the
        real values. Base returns (None, None) which disables abort push.
        """
        return (None, None)
