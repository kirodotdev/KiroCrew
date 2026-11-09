"""Remote gateway: the ``connected_computer`` Session Environments provider.

A chat bound to a connected computer has a choice row with provider
:data:`PROVIDER_ID` and the device id as its configuration. It is prepared
beside the Coder and VM providers, never through them: the owner's computer is
never a disposable VM, so no registry row, no archive stop, no retention sweep
and no deletion path exists for it (B12). The row itself is the binding:

* the chat always runs on that one device id; an offline computer refuses the
  start and the chat stays bound to it, never moving to the gateway or to
  another computer (B10);
* a revoked or removed computer refuses the start with that reason (B07, B12);
* the feature switch is checked at every start (flag scenarios).
"""

from __future__ import annotations

import asyncio
from typing import Any, Final, Optional

from kiro_crew.connected_computers.broker import ConnectionBroker, DeviceOffline, get_broker
from kiro_crew.connected_computers.devices import STATUS_REMOVED, DeviceRegistry
from kiro_crew.connected_computers.flag import require_remote
from kiro_crew.connected_computers.launch import ConnectedComputerLaunch
from kiro_crew.session_environment import SessionEnvironmentUnavailable

#: The choice row's provider tag. Not a ``config.session_environments`` provider:
#: a computer is enrolled, not configured, so it has no profile list.
PROVIDER_ID: Final[str] = "connected_computer"
#: Shown to the model instead of the Coder workspace wording.
_NOTICE: Final[str] = (
    "\n\nThis session's files and shell are on the user's own computer {label}, in "
    "{root}. Use the @workspace tools for every file and command; the gateway's own "
    "file and shell tools are not available. Only that folder can be read or "
    "changed, and commands run confined to it. Git uses that computer's own "
    "logins, if it has any reachable from that folder. Do not go looking for keys "
    "or tokens."
)


def notice_for(label: str, root: str) -> str:
    return _NOTICE.format(label=label, root=root)


async def prepare_connected_session(
    session_key: str,
    device_id: str,
    *,
    registry: Optional[DeviceRegistry] = None,
    broker: Optional[ConnectionBroker] = None,
) -> Any:
    """A prepared session on ``device_id``, or raise why it cannot start now."""
    from kiro_crew.coder.session_launch import PreparedRemoteSession
    from kiro_crew.connected_computers.vault_doc import VaultDocError, open_vault

    require_remote()
    try:
        reg = registry if registry is not None else DeviceRegistry(open_vault())
        record = await asyncio.to_thread(reg.get, device_id)
    except VaultDocError as exc:
        raise SessionEnvironmentUnavailable(str(exc)) from exc
    if record is None:
        raise SessionEnvironmentUnavailable(
            "this chat's computer is not enrolled on this gateway; it is not moved elsewhere"
        )
    if record.status == STATUS_REMOVED:
        raise SessionEnvironmentUnavailable(
            f"this chat's computer {record.label} was removed from this gateway; its files "
            "were not touched. Start a new chat to use another computer"
        )
    if not record.active:
        raise SessionEnvironmentUnavailable(
            f"this chat's computer {record.label} was disconnected and its access revoked"
        )
    brk = broker if broker is not None else get_broker()
    conn = brk.live(device_id)
    if conn is None:
        raise DeviceOffline(
            f"this chat's computer {record.label} is offline; connect it from that "
            "computer to continue. The chat stays on that computer"
        )
    launch = ConnectedComputerLaunch(
        device_id,
        record.label,
        session_key=session_key,
        exposed_mcp=conn.mcp,
        broker=brk,
    )
    return PreparedRemoteSession(
        launch,  # type: ignore[arg-type]  # a duck-typed launch: see launch.py
        conn.root,
        record.label,
        device_id,
        record.label,
        created=False,
    )
