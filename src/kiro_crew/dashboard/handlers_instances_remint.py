"""The answer for a re-mint that returned no token, for ``handlers_instances``.

``connect`` (a stored token that did not confirm) and ``refresh-token`` both ask the
manager for a fresh mint and get ``None`` back when it fails. The manager keeps the
mint's own error (:meth:`SshTunnelManager.last_refresh_error`), so the answer names
what failed rather than a guess. A refused SSH sign-in gets its own wording and code:
the tunnel can read connected while every mint meets the refusal, and the person has
to renew a key or certificate, not look at the remote gateway.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.instances.token_mint import SshAuthRefusedError


def remint_failure(mgr: Any, instance_id: str, fallback: str) -> tuple[str, str]:
    """``(error, code)`` for a failed re-mint; ``code`` is ``""`` unless the mint named one.

    *fallback* is the route's own wording, used when the manager kept no reason (the
    crew was not connected, a reconfiguration was under way). The mint's error text
    is ssh's redacted stderr, already bounded by the manager, and never holds a token.
    """
    failure = mgr.last_refresh_error(instance_id)
    if failure is None:
        return fallback, ""
    code, text = failure
    if code == SshAuthRefusedError.code:
        return (
            "the crew's host refused the SSH key or certificate, so its token could "
            f"not be re-minted. Renew it, then connect again ({text})",
            code,
        )
    return f"{fallback} ({text})", ""
