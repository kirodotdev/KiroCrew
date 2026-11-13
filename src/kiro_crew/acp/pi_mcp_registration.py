"""Read the pi session's own MCP registration receipt from its scratch window.

pi-acp drops the ``session/new`` array, so Crew's servers reach pi only through
the gate extension's handoff file, and only the session's own pi process can say
what it registered. Its ``session_start`` handler writes a receipt here; the host
reads it before the first prompt. The three outcomes and why an unconfirmed one
never reaches the failed bucket: ``docs/system-specs/modules/harness-parity.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

logger = logging.getLogger(__name__)

REGISTRATION_ENV = "KIROCREW_PI_MCP_RECEIPT"
REGISTRATION_MARKER = "kiro-crew-mcp-registration"
# The receipt shape this build's extension writes; any other value is invalid.
REGISTRATION_VERSION = 1

# The failed-row reason for a server the extension did not register, by the cause
# it named. The panel prints each after "Failed to start in this session:".
UNREGISTERED_REASONS = {
    "mcp_json": (
        "pi's own mcp.json declares a server of the same name, which outranks the one "
        "Kiro Crew placed. Rename or remove that entry"
    ),
    "mcp_json_unreadable": (
        "a pi mcp.json file exists but cannot be read, so Kiro Crew cannot rule out a "
        "same-named entry there. Fix or remove that file"
    ),
    "refused": (
        "pi refused to register it: another pi extension may hold the same name. "
        "Rename or remove that server"
    ),
    "replaced": (
        "pi holds a different server under this name, so Kiro Crew's tools are refused. "
        "Rename or remove that server"
    ),
}
# The failed-row reason for every handed-off server when the extension says the
# handoff gave it none, by the status it named.
HANDOFF_REASONS = {
    "none": "pi was not told where Kiro Crew's server list was. Restart the session",
    "unreadable": "pi could not read the server list Kiro Crew handed it. Restart the session",
    "malformed": "pi found the server list Kiro Crew handed it malformed. Restart the session",
    "unsealed": (
        "the server list pi read was not sealed for this session (another session's, or "
        "damaged). Restart the session"
    ),
}
_HANDOFF_LOADED = "loaded"
_RECEIPT_CAP = 131_072


class Registration(NamedTuple):
    """One valid receipt: how the handoff read went, and the per-server answer."""

    handoff: str
    registered: tuple[str, ...]
    unregistered: dict[str, str]


def registration_path(scratch: str | Path | None, nonce: str) -> Path | None:
    """The receipt lives in the scratch window already exposed to this child."""
    if not scratch or not nonce:
        return None
    return Path(scratch) / f"kirocrew_pi_mcp_{nonce}.json"


def discard_registration(scratch: str | Path | None, nonce: str) -> None:
    """Clear the previous child's receipt before a new/load retry. Blocking."""
    path = registration_path(scratch, nonce)
    if path is not None:
        with suppress(OSError):
            path.unlink(missing_ok=True)


def read_registration(scratch: str | Path | None, nonce: str) -> Registration | None:
    """What this session's pi process says it registered, or None when unknown.

    None for a missing scratch window, a missing or unreadable receipt, one for
    another nonce or another shape, or a cause or status this build does not
    know. The receipt is removed either way: it is read once, for this session
    attempt. Blocking.
    """
    path = registration_path(scratch, nonce)
    if path is None:
        return None
    # Other agents of the session tree can write this window: a FIFO here must
    # not hang the session start, so the open never blocks and only a file is read.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = os.read(fd, _RECEIPT_CAP)
        finally:
            os.close(fd)
        body = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        with suppress(OSError):
            path.unlink(missing_ok=True)
    if not isinstance(body, dict):
        return None
    registered = body.get("registered")
    unregistered = body.get("unregistered")
    handoff = body.get("handoff")
    if not (
        body.get(REGISTRATION_MARKER) == REGISTRATION_VERSION
        and body.get("nonce") == nonce
        and isinstance(handoff, str)
        and (handoff == _HANDOFF_LOADED or handoff in HANDOFF_REASONS)
        and isinstance(registered, list)
        and all(isinstance(name, str) for name in registered)
        and isinstance(unregistered, dict)
        and all(
            isinstance(cause, str) and cause in UNREGISTERED_REASONS
            for cause in unregistered.values()
        )
        and not set(registered) & set(unregistered)
    ):
        return None
    return Registration(handoff, tuple(dict.fromkeys(registered)), dict(unregistered))


async def verify_registration(client: AcpClient) -> None:
    """Record what this pi session's own process says about its handed-off servers.

    Runs after ``session/new`` or ``session/load`` and before the first prompt,
    without submitting one. A handoff of no servers has nothing to confirm. No
    outcome refuses the session.
    """
    expected = tuple(client._pi_mcp_server_names)
    scratch = client.work_scratch_dir
    receipt = await asyncio.to_thread(read_registration, scratch, client._pi_gate_nonce)
    if not expected:
        return
    if not scratch or receipt is None:
        logger.warning(
            "pi session %s: registration of %d handed-off MCP servers unconfirmed (%s)",
            client._session_id,
            len(expected),
            "no scratch window" if not scratch else "no valid receipt",
        )
        return
    if receipt.handoff != _HANDOFF_LOADED:
        failed = dict.fromkeys(expected, HANDOFF_REASONS[receipt.handoff])
    else:
        failed = {
            name: UNREGISTERED_REASONS[receipt.unregistered[name]]
            for name in expected
            if name in receipt.unregistered
        }
    if failed:
        logger.warning(
            "pi session %s: %d of %d handed-off MCP servers not registered (handoff %s)",
            client._session_id,
            len(failed),
            len(expected),
            receipt.handoff,
        )
        # Past the failed bucket's cap a row is dropped, as the mount check's are.
        for name, reason in failed.items():
            client._mcp_report.record_pruned(name, reason)
