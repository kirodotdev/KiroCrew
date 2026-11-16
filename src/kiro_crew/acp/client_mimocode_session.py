"""The MiMo Code session contract ``MimocodeAcpClient`` keeps.

MiMo Code, with no sign-in, no Xiaomi key and no ``model`` in its config, opens a
session on Xiaomi's hosted model and sends the first prompt there with no credential.
This module holds the session's answer: it refuses that session before its first
prompt (``acp/no_model_provider.py``).

Each function here whose first parameter is ``self`` is a ``MimocodeAcpClient`` method:
the class binds it by name, so it reads the session's own state on the instance, and a
patch of the method on the class or on an instance reaches every caller.

Instance fields written: none.

Instance fields read: ``_resolved_model_id``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.acp.no_model_provider import (
    AcpNoModelProvider,
    is_mimocode_hosted_fallback,
    mimocode_hosted_fallback_message,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.client import MimocodeAcpClient


# The ``AcpClient`` hook ``MimocodeAcpClient`` fills.


def _refuse_harness_default_model(self: MimocodeAcpClient) -> None:
    """The refusal of a session left on Xiaomi's hosted model with no sign-in.

    Before the first prompt; see :func:`is_mimocode_hosted_fallback` for the test. The
    credential fact is the read-back's presence check, never the file's contents.
    """
    current = self._resolved_model_id or ""
    read_back = self._harness_read_back()
    if not is_mimocode_hosted_fallback(
        current,
        self._advertised_model_ids(),
        read_back.config_model,
        read_back.hosted_credential,
    ):
        return
    shown, _ = redact_exfiltration_urls(current)
    shown, _ = redact_credentials(shown)
    raise AcpNoModelProvider(mimocode_hosted_fallback_message(shown))
