"""Local machine: turn one native consent into one grant (the shared share path).

Two native consents reach here, and only them: the local gateway's redeem route
(the desktop shell's main process, after its dialog for a page-minted consent id;
:mod:`.local_routes`) and the desktop app's own host (the native menu, for a
window on a remote gateway; :mod:`.desktop_host`). Both run the same steps in
the same order, so a grant has one authority whichever way it was asked for:

1. the OS can protect the credential (R2) -- before anything reaches the remote;
2. the root and the ticked local tools pass this computer's own policy (B05/B14);
3. the SSH enrollment, under the controller's ``pair_lock``;
4. the grant is written in the same vault write that checks the Off generation,
   so an Off made meanwhile by any process leaves no grant (R8); otherwise the
   remote enrollment is undone, or queued when unreachable.

The Off generation is the one the caller read when the consent BEGAN (before its
native dialog opened, :func:`read_off_generation`), never one read here: an Off
made while the dialog was open must refuse the grant it would otherwise write.

Callers check the switch and the consent themselves; ``enabled`` is re-read after
the enrollment.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable

from kiro_crew.connected_computers.local_store import Grant


class ShareRefused(Exception):
    """Nothing was shared; ``message`` is safe to show the person."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


async def read_off_generation(store: Any) -> int:
    """The Off generation a consent starts from; raise ShareRefused when unreadable."""
    from kiro_crew.connected_computers.local_store import ProtectionUnavailable
    from kiro_crew.connected_computers.vault_doc import VaultDocError

    try:
        await asyncio.to_thread(store.protection_ready)
        return int(await asyncio.to_thread(store.off_generation))
    except ProtectionUnavailable as exc:
        raise ShareRefused(503, "protection_unavailable", str(exc)) from exc
    except VaultDocError as exc:
        raise ShareRefused(409, "protection_broken", str(exc)) from exc


async def _forget_enrolled(instance: Any, device_id: str, fingerprint: str) -> bool:
    """Undo an enrollment no grant was written for; False when not confirmed."""
    from kiro_crew.connected_computers import enroll as enroll_mod

    try:
        await enroll_mod.forget(instance, device_id=device_id, fingerprint=fingerprint)
    except Exception:  # noqa: BLE001 -- no local grant exists either way
        return False
    return True


async def share_computer(
    controller: Any,
    *,
    instance_id: str,
    instance_lookup: Callable[[str], Any],
    root: Any,
    mcp: Any,
    enabled: Callable[[], bool],
    off_generation: int,
) -> Grant:
    """Enroll this computer with ``instance_id`` and write its grant; raise ShareRefused.

    ``off_generation`` is what :func:`read_off_generation` returned when the
    consent began; a local Off since then refuses the grant.
    """
    from kiro_crew.connected_computers import enroll as enroll_mod
    from kiro_crew.connected_computers import local_mcp
    from kiro_crew.connected_computers.identity import (
        hostname_slug,
        install_fingerprint,
        local_computer_name,
    )
    from kiro_crew.connected_computers.local_store import (
        ProtectionUnavailable,
        TurnedOff,
        new_grant_id,
    )
    from kiro_crew.connected_computers.root_policy import RootRefused, resolve_root
    from kiro_crew.connected_computers.vault_doc import VaultDocError

    if not isinstance(mcp, list) or not all(isinstance(n, str) for n in mcp):
        raise ShareRefused(400, "invalid_request", "mcp must be a list of names")
    store = controller.store
    try:
        await asyncio.to_thread(store.protection_ready)
    except ProtectionUnavailable as exc:
        raise ShareRefused(503, "protection_unavailable", str(exc)) from exc
    except VaultDocError as exc:
        raise ShareRefused(409, "protection_broken", str(exc)) from exc
    try:
        real_root = await asyncio.to_thread(resolve_root, root if isinstance(root, str) else "")
        exposed = local_mcp.snapshot(mcp, await asyncio.to_thread(local_mcp.configured_servers))
    except (RootRefused, local_mcp.McpExposureRefused) as exc:
        raise ShareRefused(400, "refused", str(exc)) from exc
    instance = await asyncio.to_thread(instance_lookup, instance_id)
    if instance is None:
        raise ShareRefused(404, "unknown_instance", "that Remote Crew connection is gone")
    try:
        key = await asyncio.to_thread(store.install_key)
    except ProtectionUnavailable as exc:
        raise ShareRefused(503, "protection_unavailable", str(exc)) from exc
    except VaultDocError as exc:
        raise ShareRefused(409, "protection_broken", str(exc)) from exc
    fingerprint = install_fingerprint(key)
    async with controller.pair_lock:
        try:
            enrolled = await enroll_mod.enroll(
                instance,
                base=hostname_slug(local_computer_name()),
                fingerprint=fingerprint,
                label=local_computer_name(),
            )
        except enroll_mod.EnrollmentFailed as exc:
            raise ShareRefused(502, "enrollment_failed", str(exc)) from exc
        try:
            # Off the loop: without a live snapshot the switch is read from disk.
            if not await asyncio.to_thread(enabled):
                raise TurnedOff("access was turned off on this computer meanwhile")
            grant = await asyncio.to_thread(
                functools.partial(
                    store.add,
                    Grant(
                        grant_id=new_grant_id(),
                        instance_id=instance_id,
                        gateway_id=enrolled.gateway_id,
                        gateway_label=enrolled.gateway_label,
                        device_id=enrolled.device_id,
                        root=real_root,
                        mcp=exposed,
                        credential=enrolled.credential,
                        credential_expires=enrolled.expires,
                        peer_key=enrolled.peer_key,
                    ),
                    expect_off_generation=off_generation,
                )
            )
        except (TurnedOff, ProtectionUnavailable, VaultDocError) as exc:
            if not await _forget_enrolled(instance, enrolled.device_id, fingerprint):
                await asyncio.to_thread(
                    store.queue_forget, instance_id, enrolled.gateway_id, enrolled.device_id
                )
            if isinstance(exc, TurnedOff):
                raise ShareRefused(409, "turned_off", str(exc)) from exc
            if isinstance(exc, ProtectionUnavailable):
                raise ShareRefused(503, "protection_unavailable", str(exc)) from exc
            raise ShareRefused(409, "protection_broken", str(exc)) from exc
    controller.start()
    await controller.reconcile()
    return grant
