"""The warm pool's hold while Kiro starts fail because kiro-cli is signed out.

``AcpAuthRequired`` is non-retryable: every start launches a runtime, fails at
``session/new`` and kills it, until someone signs in again. The pool's health sweep
refills on every tick, so without a hold a signed-out host starts and kills a
runtime every sweep for as long as the sign-in stays lapsed.

A failed pool start records the hold for its backend (:func:`note_signed_out`). A
start on the same backend that gets a session releases it (:func:`note_signed_in`), so a chat turn after sign-in
lets the pool fill on the next sweep. With nothing else starting, the pool retries
once the hold's backoff runs out, and each further failure doubles it up to
:data:`_HOLD_MAX_SECS`, so a sign-in nothing else observes is still picked up.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

#: The first hold after a signed-out start, then doubled per failure up to the cap.
_HOLD_FIRST_SECS = 60.0
_HOLD_MAX_SECS = 900.0

#: Per backend id, ``(monotonic deadline, current backoff)`` while held. Keyed so
#: one harness's sign-in neither holds nor releases another harness's pool.
_HELD: dict[str, tuple[float, float]] = {}


def note_signed_out(backend: str) -> None:
    """Hold the warm pool for *backend* after a start failed because it is signed out."""
    record = _HELD.get(backend)
    backoff = _HOLD_FIRST_SECS if record is None else min(record[1] * 2, _HOLD_MAX_SECS)
    if record is None:
        logger.warning(
            "Backend %r is signed out; the warm pool waits for a sign-in before it starts again",
            backend,
        )
    _HELD[backend] = (time.monotonic() + backoff, backoff)


def note_signed_in(backend: str) -> None:
    """Release *backend*'s hold: a start on it got a session, so sign-in is back."""
    if _HELD.pop(backend, None) is not None:
        logger.info("Backend %r sign-in is back; the warm pool starts again", backend)


def held(backend: str) -> bool:
    """Whether the warm pool should not start a *backend* runtime now. No IO.

    True until the current backoff runs out. The record stays after that, so the
    next failure doubles the backoff and a success releases it.
    """
    record = _HELD.get(backend)
    return record is not None and time.monotonic() < record[0]
