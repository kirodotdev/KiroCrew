"""Per-process boot identifier for sessions that must not outlive the gateway.

Lives outside ``token_auth`` so ``refresh_tokens`` can consult it without a
``token_auth`` <-> ``refresh_tokens`` import cycle — the same seam
``token_secret.py`` provides for the HMAC secret and ``revocation_gen.py`` for
the revocation counter.

This is the deliberate MIRROR IMAGE of ``revocation_gen``. That counter is
persisted precisely so sessions survive a restart without logging anyone out.
This value is never written anywhere, so a token carrying it is worthless the
moment the process ends. Both exist because "how long may this session live"
has two different right answers depending on where the credential went:

* A browser on the operator's own machine should not be logged out by a
  gateway restart — hence the persisted counter and the persisted HMAC secret.
* A credential handed to a device the dashboard cannot identify (a phone that
  scanned a QR code) is bounded instead by something the operator can see and
  act on. Tying it to process lifetime means "my gateway is up, my phone
  works", and a restart is a hard revoke that needs no state to be recorded.

The binding is CLAIM-GATED, not global: only a token minted with the ``boot``
claim is checked against this value, so every existing session — and every
session minted without opting in — behaves exactly as before.

Not a security boundary on its own. It bounds a session's LIFETIME; it says
nothing about who the holder is. Identity still comes from the signed subject,
and the peer pin, the revocation counter and the per-session nonce denylist all
still apply unchanged.
"""

from __future__ import annotations

import secrets
import threading

# Memoized per-process value. ``None`` = not yet generated. Guarded by the lock
# so two concurrent first readers cannot mint two different ids for one process
# — which would make a session issued by one request unverifiable by the next.
_boot_id: str | None = None
_boot_lock = threading.Lock()


def current_boot_id() -> str:
    """Return this process's boot id, generating it on first use.

    Generated lazily rather than at import time for the same reason
    ``revocation_gen`` loads lazily: the CLI imports the dashboard auth modules
    transitively for every ``kirocrew`` subcommand, and importing a module must
    not have side effects. Unlike that module the cost here is not filesystem
    I/O but entropy plus the guarantee that a short-lived CLI process never
    mints an id nobody will ever validate against.

    Deliberately has no ``_or_none`` variant. ``revocation_gen`` needs one
    because its answer lives on disk and a failed READ must fail closed rather
    than silently authenticate a revoked session. This value cannot fail to be
    read: it is in memory, and if the process is gone so is every session bound
    to it. There is nothing to fail closed about.
    """
    global _boot_id
    with _boot_lock:
        if _boot_id is None:
            _boot_id = secrets.token_hex(16)
        return _boot_id
