"""The ``AcpClient`` start error for a scope that systemd did not admit in time.

``client_start._admit_scope`` raises it when the user systemd manager has not moved
the spawn's pid into its scope within the bound (see ``scope_admission``).
"""

from __future__ import annotations

from kiro_crew.acp.transport_errors import AcpError


class AcpScopeNotAdmitted(AcpError):  # noqa: N818
    """The user systemd manager did not admit the agent's scope within its bound.

    The ``AcpClient`` twin of the runtime's ``AcpScopeAdmissionTimeout``, in the
    ``AcpError`` family so that every caller of the client handles it.
    ``transient`` is fixed ``False`` for the same reason: the cause is a manager
    that is behind, and each retry asks it for one more transient unit. A distinct
    type, so ``ensure_ready`` can end the start without its one retry.
    ``user_worded`` is ``True``: the text is fixed Kiro Crew wording from
    ``scope_admission.timeout_message``, so a chat surface shows the diagnosis
    instead of its generic "try again" reply.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, transient=False, user_worded=True)
