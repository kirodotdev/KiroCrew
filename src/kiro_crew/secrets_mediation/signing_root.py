"""Minting the mediated-secret policy-signing root once the gateway listener serves.

Kicked by ``dashboard.server_runtime.maintenance._kick_mediation_signing_root_mint``
from both entrypoints, strictly after the bind: minting touches the data-home
filesystem (``make_owner_only_dir`` / ``atomic_write`` / ``os.link``), and a stalled
or full data home must never block the listener. Mediation is FAIL-CLOSED until it
lands: ``provenance._secret_policy_key`` returns ``None`` while the root is absent,
so ``verify_secret_authorizations`` refuses every request that arrives first.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.secrets_mediation.provenance import initialize_secret_host_key

logger = logging.getLogger(__name__)


def _sel_fn() -> Any:
    """Late-bound ``sel()`` so tests can substitute it and SEL init stays lazy."""
    from kiro_crew.sel import sel

    return sel()


async def mint_mediation_signing_root() -> None:
    """Mint (or load) the certified root; DEGRADE rather than fail-stop.

    An occupied-but-uncertifiable ``key.json`` is reachable with no tampering by
    the fenced agent: a pre-upgrade plant under a then-unmasked leaf, a partial
    data-home restore, or an operator deleting ``token_signing.key`` (whose
    recreation yields a token root that does not certify the existing document).
    On first boot the key is absent and minted, and its write paths raise a bare
    ``OSError`` on a read-only or full data home. Halting buys no security in
    either case -- verification already fails closed with the root unloaded --
    so the degradation is audited and mediation stays disabled-closed.
    """
    try:
        await asyncio.to_thread(initialize_secret_host_key)
    except (RuntimeError, OSError) as exc:
        detail = str(exc)

        def _audit_degradation() -> None:
            # Best-effort ONLY: the trigger is often the same unwritable or full
            # data home SEL's own first-access init writes to, and an audit that
            # can crash would defeat degrading. The posture does not depend on
            # this landing.
            try:
                _sel_fn().log_api_access(
                    caller="gateway",
                    operation="mediated_secret_signing_key_init",
                    outcome="degraded",
                    source="token_auth",
                    resources=(
                        "the mediated-secret policy signing root could not be "
                        "initialized or certified; running with custom-secret "
                        "mediation DISABLED-CLOSED (policy verification fails "
                        f"closed): {detail}"
                    ),
                )
            except Exception:
                logger.warning(
                    "mediated-secret signing root degraded and the degradation "
                    "audit could not be written (SEL unavailable on this data "
                    "home); running with mediation DISABLED-CLOSED: %s",
                    detail,
                )

        # SEL's first access does bounded filesystem init: keep it off the loop.
        await asyncio.to_thread(_audit_degradation)
