"""The error a kiro slash command raises when kiro-cli answers it ``success: false``."""

from __future__ import annotations

from kiro_crew.acp.transport_errors import AcpError


class AcpCommandRefused(AcpError):
    """kiro-cli answered a command (an ``/effort`` level) ``success: false``.

    It sends no JSON-RPC error for this, so ``send_command`` raises it from the
    answer. ``command_refused`` is the tag callers read: a reset replays the
    refusal, so a caller keeps the session or answers 4xx.
    """

    command_refused = True
