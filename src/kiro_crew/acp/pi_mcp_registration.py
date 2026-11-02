"""Read the actual pi child's MCP registration receipt from its scratch window.

pi-acp drops the ``session/new`` array, so Crew's servers reach pi only through
the gate extension's handoff file. The routing read-back runs a separate pi child
without that file, so only the session's own process can say what it registered:
its ``session_start`` handler writes, here, how reading the handoff went and, per
server, whether it registered it and why not.

Three outcomes per handed-off server, kept apart because their readers act on
them differently:

* **Registered** -- the receipt names it. Nothing is recorded; the server's own
  frames and the mount check speak for it from here.
* **Confirmed unregistered** -- the receipt says the handoff did not load, or
  names a cause for this server. A FAILED row with that cause, which is what it
  is: a server this session cannot use.
* **Unconfirmed** -- no scratch window, or no valid receipt, or a receipt that
  does not account for the server. A WARNING log line and nothing on the report,
  never a failed row: every reader of the failed bucket, down to a subagent told
  not to retry, takes it as certainly unavailable, and the server may well be
  registered. The card already shows such a server as not reported, which is
  the honest reading. That the receipt lands before ``session/new`` returns is
  read from pi-acp's source, not pinned by any live run, so its absence proves
  nothing.

The session-open mount check (:mod:`kiro_crew.acp.mcp_mount_check`) answers a
different question for a subset of these: whether a pooled BROKER STUB reached
gatewayd within its window. It cannot see a member mount pi execs directly, it
cannot say why a stub never arrived, and it reports up to its window after the
session opens, after a subagent's one spawn-time reading. The receipt is in hand
before the first prompt and names the cause, and the mount check leaves any
server this records alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

logger = logging.getLogger(__name__)

REGISTRATION_ENV = "KIROCREW_PI_MCP_RECEIPT"
REGISTRATION_MARKER = "kiro-crew-mcp-registration"
# The receipt shape this build's extension writes; any other value is invalid.
REGISTRATION_VERSION = 2

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
    try:
        with path.open(encoding="utf-8") as receipt:
            body = json.loads(receipt.read(_RECEIPT_CAP))
    except (OSError, ValueError):
        return None
    finally:
        path.unlink(missing_ok=True)
    if not isinstance(body, dict):
        return None
    registered = body.get("registered")
    unregistered = body.get("unregistered")
    handoff = body.get("handoff")
    if not (
        body.get(REGISTRATION_MARKER) == REGISTRATION_VERSION
        and body.get("nonce") == nonce
        and (handoff == _HANDOFF_LOADED or handoff in HANDOFF_REASONS)
        and isinstance(registered, list)
        and all(isinstance(name, str) for name in registered)
        and isinstance(unregistered, dict)
        and all(cause in UNREGISTERED_REASONS for cause in unregistered.values())
        and not set(registered) & set(unregistered)
    ):
        return None
    return Registration(str(handoff), tuple(dict.fromkeys(registered)), dict(unregistered))


def _record_failed(client: AcpClient, failed: dict[str, str]) -> None:
    """Record each confirmed-unregistered server as failed, counting the overflow."""
    report = client._mcp_report
    over_cap = 0
    for name, reason in failed.items():
        if not report.record_pruned(name, reason) and not report.has_reported(name):
            over_cap += 1
    if over_cap:
        report.record_unregistered_omitted(report.unregistered_omitted + over_cap)


async def verify_registration(client: AcpClient) -> None:
    """Record what this pi session's own process says about its handed-off servers.

    Runs after ``session/new`` or ``session/load`` and before the first prompt,
    without submitting one: pi-acp does not settle a prompt an extension command
    handles. A handoff of no servers has nothing to confirm. No outcome refuses
    the session; see the module docstring for the three outcomes.
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
        unaccounted: tuple[str, ...] = ()
    else:
        failed = {
            name: UNREGISTERED_REASONS[receipt.unregistered[name]]
            for name in expected
            if name in receipt.unregistered
        }
        unaccounted = tuple(
            name for name in expected if name not in failed and name not in receipt.registered
        )
    if failed:
        logger.warning(
            "pi session %s: %d of %d handed-off MCP servers not registered (handoff %s)",
            client._session_id,
            len(failed),
            len(expected),
            receipt.handoff,
        )
        _record_failed(client, failed)
    if unaccounted:
        # A valid receipt that is silent on a server confirms nothing about it.
        logger.warning(
            "pi session %s: receipt does not account for %d handed-off MCP servers",
            client._session_id,
            len(unaccounted),
        )
