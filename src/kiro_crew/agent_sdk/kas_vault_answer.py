"""What a host lends a pod of its own KAS sign-in answer.

The agent-SDK boundary in front of ``acp.kas_host_auth``'s vault-only half: the
token answer and the refused question, read from this data home's Crew vault
alone. Only ``dashboard.handlers.pod_host_sign_in`` imports it.
"""

from __future__ import annotations

from kiro_crew.acp.kas_host_auth import (
    HostAuthCallbackError,
    HostNotSignedIn,
    answer_from_vault,
    vault_sign_in_refused,
)

__all__ = [
    "HostAuthCallbackError",
    "HostNotSignedIn",
    "answer_from_vault",
    "vault_sign_in_refused",
]
