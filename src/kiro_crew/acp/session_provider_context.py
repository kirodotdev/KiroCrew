"""The view of one ``AcpSessionProvider`` that the context builder reads.

The ``ContextPromptProvider`` facts (``kiro_crew.agent_sdk.context``): which
session incarnation a prompt was built for, the provider label, the documents
the session was launched with, whether its agent prompt carries a session head
(``kiro_crew.session_head``), whether it holds the member tools, and whether it
loads steering and reports compactions itself.

Each function here is an ``AcpSessionProvider`` property body: the class binds it
with ``property(...)``, so it reads the provider's own state on the instance.

Instance fields read: ``_handle``, ``backend``, ``session_id``, ``process_instance``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKENDS_COMPACTION_REPORTED

if TYPE_CHECKING:
    from kiro_crew.acp.session_provider import AcpSessionProvider


def context_incarnation(self: AcpSessionProvider) -> object:
    return (id(self._handle), self.session_id, self.process_instance)


def context_provider_type(self: AcpSessionProvider) -> str:
    from kiro_crew.providers.acp import provider_label

    return provider_label(self)


def native_context_documents(self: AcpSessionProvider) -> dict[str, str]:
    fit = getattr(self._handle, "native_context_documents_fit", None)
    if fit is not None and not fit():
        return {}
    return dict(self._handle.native_context_documents)


def session_head_held(self: AcpSessionProvider) -> bool:
    # The raw launch record, no fit check: the wire prompt carried the head
    # whatever the declared files weigh today. A dict read, safe on the loop.
    from kiro_crew import session_head

    return session_head.applied(self._handle.native_context_documents)


def member_dispatch_mounted(self: AcpSessionProvider) -> bool | None:
    # A handle that never recorded an answer is no evidence, not a refusal.
    mounted = getattr(self._handle, "member_dispatch_mounted", None)
    return None if mounted is None else bool(mounted)


def native_steering(self: AcpSessionProvider) -> bool:
    return self.backend == ACP_BACKEND_KAS


def reports_compaction(self: AcpSessionProvider) -> bool:
    return self.backend in ACP_BACKENDS_COMPACTION_REPORTED
