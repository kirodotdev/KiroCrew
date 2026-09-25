"""Operator-defined ACP backends: the ADDITIVE step that registers them.

Harness support is additive at the platform seam (harness-parity H13): the Kiro
construction path -- ``bootstrap_context`` and everything it calls -- gains no
conditional, no I/O and no failure mode in service of an adapter. Loading the
operator's ``harnesses.json`` IS adapter work: a file read, descriptor
validation, registry writes, and a re-resolution of the configured default
against the widened registry. So it does not live in ``bootstrap_context`` or in
the public edition's ``ProviderRegistry.register_acp_backends`` (which stays the
no-op it is upstream). It lives here, as one function the GATEWAY calls after
``boot_platform`` returns, and nothing else calls: a CLI command, an app server
or a test that boots the platform runs exactly the Kiro path it ran before.

Ordering, and why each half is here rather than in bootstrap:

* Descriptors register AFTER ``bootstrap_context``'s ``agent_backend``
  governance narrowing ran, so the narrowing is re-applied here for the ids just
  registered (``narrow_selectable_backends``) -- the same re-application
  ``mark_routing_verified`` does for a backend verified after boot. A
  policy-denied descriptor stays unselectable with the policy reason.
* The config instance the gateway holds was loaded BEFORE registration, so its
  ``agent.acp_backend`` was coerced against a registry without the operator ids
  and degraded to Kiro. The load kept the file's own spelling beside the coerced
  field (``AgentConfig.acp_backend_persisted``); it is re-resolved here through
  the one selection gate -- an in-memory registry lookup, never a second read of
  ``config.json``. On a deployment with no descriptors the lookup answers what
  the load already answered and the field is left untouched.

Best-effort throughout, like the platform seam it stands beside: a failure here
leaves the builtin harnesses serving, which is a startable deployment, and is
logged rather than raised. Synchronous file I/O; the gateway runs it through
``asyncio.to_thread``.

The settle gate. Because the step runs in the background, there is a window
after the dashboard binds in which the gateway's ``agent.acp_backend`` still
reads the boot-time coercion (Kiro) while ``config.json`` names an operator
backend that is about to register. An UNPINNED chat that dispatched in that
window would run its prompt on Kiro -- the wrong provider, silently. So the
gateway announces the registration BEFORE it schedules it
(:func:`registration_pending`), the step marks it settled when it returns, on
every path (:func:`registration_settled` reads it), and unpinned provider
allocation waits for that (:func:`wait_until_registration_settled`) -- a pinned
chat does not, its backend is its own. With no gateway announcing anything
(a CLI command, an app server, a test) the gate is settled from import, so
nothing outside a gateway ever waits.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

#: Set when no operator-backend registration is pending. Set from import (nothing
#: is pending until a gateway announces a registration); cleared by
#: :func:`registration_pending`; set again by :func:`register_operator_backends`
#: on every exit path and by the gateway's task wrapper on cancellation. A
#: ``threading.Event`` because the step runs in a worker thread and the readers
#: are on the loop; the awaitable wait polls it rather than binding a loop.
_REGISTRATION_SETTLED = threading.Event()
_REGISTRATION_SETTLED.set()

#: How often the awaitable wait re-checks the gate, in seconds.
_SETTLE_POLL_SECS = 0.05


def registration_pending() -> None:
    """Announce that a registration is about to run: unpinned dispatch waits from now."""
    _REGISTRATION_SETTLED.clear()


def mark_registration_settled() -> None:
    """Release the gate. Called by the step itself; also by a caller whose scheduled
    step will not run (a cancelled task), so no chat waits forever."""
    _REGISTRATION_SETTLED.set()


def registration_settled() -> bool:
    """True when no registration is pending (the default outside a gateway)."""
    return _REGISTRATION_SETTLED.is_set()


async def wait_until_registration_settled() -> None:
    """Return once the gate is released; immediately when nothing is pending."""
    while not _REGISTRATION_SETTLED.is_set():
        await asyncio.sleep(_SETTLE_POLL_SECS)


def register_operator_backends(cfg: "KiroCrewConfig") -> None:
    """Load ``harnesses.json``, register its descriptors, and settle *cfg*'s default.

    Idempotent: the descriptor loader skips ids an earlier pass registered, and
    the re-resolution assigns only when the answer differs from the field.
    Releases the settle gate on every exit path.
    """
    try:
        _register_operator_backends(cfg)
    finally:
        mark_registration_settled()


def _register_operator_backends(cfg: "KiroCrewConfig") -> None:
    try:
        from kiro_crew.agent_sdk.operator_harnesses import (
            load_and_register_operator_descriptors,
        )

        load_and_register_operator_descriptors()
    except Exception:
        logger.warning("operator backend registration failed; continuing", exc_info=True)
        return
    try:
        from kiro_crew.agent_backend_governance import narrow_selectable_backends

        narrow_selectable_backends()
    except Exception:
        logger.warning(
            "agent_backend governance could not be re-applied to operator backends; continuing",
            exc_info=True,
        )
    try:
        from kiro_crew.acp_backends import resolve_selected_backend

        resolved = resolve_selected_backend(cfg.agent.acp_backend_persisted)
        if resolved != cfg.agent.acp_backend:
            cfg.agent.acp_backend = resolved
    except Exception:
        logger.warning(
            "post-registration re-resolution of agent.acp_backend failed; continuing",
            exc_info=True,
        )
