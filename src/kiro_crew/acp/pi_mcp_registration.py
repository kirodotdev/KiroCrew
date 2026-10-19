"""Read the actual pi child's MCP registration receipt from its scratch window.

pi-acp drops the ``session/new`` array, so Crew's servers reach pi only through
the gate extension's handoff file. The routing read-back runs a separate pi child
without that file, so only the session's own process can say what it registered:
its ``session_start`` handler writes the names here before pi answers the
adapter's first RPC, which is before ``session/new`` returns.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.acp.transport_errors import AcpError

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

REGISTRATION_ENV = "KIROCREW_PI_MCP_RECEIPT"
REGISTRATION_MARKER = "kiro-crew-mcp-registration"
# The failed-row reason for a handed-off server the session did not register,
# the case the extension warns about: pi's own mcp.json (or another extension)
# holds the name, so the gate refuses that server's tools.
UNREGISTERED_REASON = (
    "pi did not register this server for the session: pi's own MCP configuration "
    "or another extension holds the same name. Rename or remove that entry."
)
_RECEIPT_CAP = 131_072


def registration_path(scratch: str | Path | None, nonce: str) -> Path | None:
    """The receipt lives in the scratch window already exposed to this child."""
    if not scratch or not nonce:
        return None
    return Path(scratch) / f"kirocrew_pi_mcp_{nonce}.json"


def discard_registration(scratch: str | Path | None, nonce: str) -> None:
    """Clear the previous child's receipt before a new/load retry. Blocking."""
    path = registration_path(scratch, nonce)
    if path is not None:
        path.unlink(missing_ok=True)


def read_registration(scratch: str | Path | None, nonce: str) -> tuple[str, ...] | None:
    """The server names this session's pi process registered, or None when unknown.

    None for a missing scratch window, a missing or unreadable receipt, one for
    another nonce, or any other shape. The receipt is removed either way: it is
    read once, for this session attempt. Blocking.
    """
    path = registration_path(scratch, nonce)
    if path is None:
        return None
    try:
        with path.open(encoding="utf-8") as receipt:
            body = json.loads(receipt.read(_RECEIPT_CAP))
    except (OSError, ValueError):
        return None
    finally:
        path.unlink(missing_ok=True)
    names = body.get("servers") if isinstance(body, dict) else None
    if not (
        isinstance(body, dict)
        and body.get(REGISTRATION_MARKER) == 1
        and body.get("nonce") == nonce
        and isinstance(names, list)
        and all(isinstance(name, str) for name in names)
    ):
        return None
    return tuple(dict.fromkeys(names))


async def verify_registration(client: AcpClient) -> None:
    """Refuse a pi session that registered none of its handed-off servers.

    Runs after ``session/new`` or ``session/load`` and before the first prompt,
    without submitting one: pi-acp does not settle a prompt an extension command
    handles. A handoff of no servers has nothing to confirm. A receipt that is
    absent, malformed, for another session, names a server that was not handed
    off, or confirms none of them refuses the session. One that
    confirms only some records the rest as failed on the session's MCP report,
    which is what the card reads, so no unregistered server is shown available.
    """
    expected = tuple(client._pi_mcp_server_names)
    confirmed = await asyncio.to_thread(
        read_registration, client.work_scratch_dir, client._pi_gate_nonce
    )
    if not expected:
        return
    if confirmed is None or not set(confirmed) <= set(expected):
        raise AcpError(
            "pi did not confirm the MCP servers handed to this session, so none of "
            "Kiro Crew's tools can be assumed available. Check that Kiro Crew's pi "
            "gate extension loads, then restart the session."
        )
    if not confirmed:
        raise AcpError(
            "pi registered none of the MCP servers handed to this session. Check "
            "pi's own mcp.json for entries with the same names, then restart the "
            "session."
        )
    for name in expected:
        if name not in confirmed:
            client._mcp_report.record_pruned(name, UNREGISTERED_REASON)
