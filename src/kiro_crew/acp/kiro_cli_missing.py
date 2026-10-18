"""The session-start refusal for a host where kiro-cli is not installed."""

from __future__ import annotations

from kiro_crew.acp.session_handle import AcpRuntimeError


class AcpKiroCliMissing(AcpRuntimeError):
    """The kiro-cli binary is not installed where the spawn looked for it.

    Raised by a kiro-cli harness's ``resolve_spawn`` before any process exists,
    with the message ``kiro_cli_not_found_message`` builds (the directories
    searched and the fix). ``kiro_cli_missing`` is the structural tag the chat
    reads to offer a route that needs no agent, such as the feature-request form,
    so that choice never depends on the prose. ``transient`` is left as the base
    class has it: whether a retry may run is the recovery layer's existing call.
    The dedicated client raises ``AcpError`` instead and sets the same tag on it.
    """

    kiro_cli_missing = True
