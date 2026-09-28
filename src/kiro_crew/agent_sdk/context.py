"""Provider-neutral observations needed by prompt delivery, not wire messages."""

from __future__ import annotations

from typing import Protocol

CONTEXT_EVENT_AGENT_CHANGED = "agent_switched"
CONTEXT_EVENT_CLEAR = "clear_status"
CONTEXT_EVENT_COMPACTION = "compaction_status"
CONTEXT_EVENT_COMPLETED = "complete"
CONTEXT_EVENT_TEXT = "text_chunk"
CONTEXT_EVENT_TOOL = "tool_call"


class ContextStreamEvent(Protocol):
    """Read-only completion evidence shared by context-capable drivers."""

    @property
    def kind(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def control_notice(self) -> bool: ...

    @property
    def stop_reason(self) -> str: ...

    @property
    def synthetic_completion(self) -> bool: ...

    @property
    def refusal(self) -> object | None: ...


class ContextPromptProvider(Protocol):
    """The narrow provider view the application prompt builder consumes.

    Obtain it through context_provider_of(), not a structural isinstance probe:
    dynamic mocks/proxies must not manufacture an opt-in through __getattr__.
    The delivery holder stays opaque here; its application owner validates it.
    """

    @property
    def essential_delivery(self) -> object: ...

    @property
    def context_incarnation(self) -> object: ...

    @property
    def context_provider_type(self) -> str: ...

    @property
    def native_context_documents(self) -> dict[str, str]: ...

    @property
    def native_steering(self) -> bool: ...

    @property
    def cwd(self) -> str: ...

    @property
    def served_model(self) -> str: ...
