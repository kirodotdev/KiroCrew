"""Persisted token-revocation generation counter shared by both cookie types.

Lives outside ``token_auth`` so ``refresh_tokens`` can consult the counter
without a ``token_auth`` <-> ``refresh_tokens`` import cycle (the same seam
``token_secret.py`` provides for the HMAC secret).

Every minted access token AND refresh token embeds the current ``gen`` claim;
validation rejects a token whose ``gen`` is below the current value. Bumping
the counter (``revoke_all_sessions()``, i.e. ``kirocrew logout``) therefore
ends ALL outstanding sessions — established access cookies and refresh chains
alike. Persisting the counter is what lets it survive a gateway restart
WITHOUT logging users out: the gen is reloaded unchanged, so previously-issued
cookies still match.

Loading is LAZY and memoized: merely importing this module must not touch the
filesystem (the CLI imports the dashboard auth modules transitively for every
``kirocrew`` subcommand). Both validators read the LIVE value through
:func:`current_revocation_gen` — never an import-time copy — so a bump is
visible to every subsequent validation in-process.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_REVOCATION_FILE = "token_revocation.gen"

# Memoized counter. ``None`` = not yet loaded from disk. Guarded by the lock so
# concurrent first reads / bumps agree on one value.
_gen: int | None = None
_gen_lock = threading.Lock()


def _warn_unreadable_counter(path: Path, reason: str, *, exc_info: bool = False) -> None:
    """Log the fail-closed recovery action without hiding its security cost."""
    logger.warning(  # nosemgrep: python-logger-credential-disclosure
        "token revocation counter file %s %s; authentication is fail-closed. "
        "To reset revocation state and restore access, delete only %s "
        "(this re-enables unexpired sessions revoked by kirocrew logout)",
        path,
        reason,
        path,
        exc_info=exc_info,
    )


def _load_revocation_gen_or_none() -> int | None:
    """Read the persisted revocation counter from disk.

    Returns the counter (0 when the file has never been written — a definitive
    answer) or ``None`` when the state is unreadable, so callers can
    distinguish "gen is 0" from "could not read": treating a failed read as 0
    would silently undo a prior revocation. An EXISTING but empty file is
    unreadable, not 0 — it is evidence of an interrupted write, and the atomic
    replace in :func:`bump_revocation_gen` means a healthy file always carries
    a complete value.
    """
    # circular import: config.loader pulls in modules that import token_auth
    # (which re-exports this module's names), so a top-level import here risks
    # a cycle. Matches the config_dir() call sites in token_secret.py.
    from kiro_crew.config.loader import config_dir

    p = Path(_REVOCATION_FILE)
    try:
        p = config_dir() / _REVOCATION_FILE
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        stripped = text.strip()
        if not stripped:
            # Logs the counter file PATH only, never any token or secret value;
            # the Semgrep rule fires on the credential-adjacent wording in the
            # static message string.
            _warn_unreadable_counter(p, "is empty (interrupted write?)")
            return None
        return int(stripped)
    except (OSError, ValueError):
        _warn_unreadable_counter(p, "could not be read", exc_info=True)
        return None


def _load_revocation_gen() -> int:
    """Read the persisted revocation counter from disk (0 if unset/unreadable)."""
    loaded = _load_revocation_gen_or_none()
    return 0 if loaded is None else loaded


def current_revocation_gen_or_none() -> int | None:
    """Return the LIVE revocation generation, or ``None`` when unreadable.

    Called on every token validation, so after the first disk read this is an
    in-memory lookup under a lock. ``warm_auth_singletons()`` primes it off the
    event loop before the server accepts connections. A FAILED disk read is not
    memoized — this call returns ``None`` and the next call retries. Validators
    MUST treat ``None`` as fail-closed (reject): revocation state that cannot
    be read must not silently authenticate a session the operator revoked.
    """
    global _gen
    with _gen_lock:
        if _gen is None:
            _gen = _load_revocation_gen_or_none()
        return _gen


def current_revocation_gen() -> int:
    """Return the LIVE revocation generation, degrading to 0 when unreadable.

    For MINT paths (embedding the claim in a new token). Validation paths must
    use :func:`current_revocation_gen_or_none` instead and reject on ``None``
    — degrading a validator to 0 would accept revoked sessions.
    """
    loaded = current_revocation_gen_or_none()
    return 0 if loaded is None else loaded


def bump_revocation_gen() -> int:
    """Increment and persist the revocation counter. Returns the new value.

    Fail-closed on I/O errors, in both directions:

    * If the persisted base cannot be READ, raises ``OSError`` without writing:
      bumping from an assumed 0 would persist a LOWER counter than on disk,
      resurrecting previously revoked sessions after a restart.
    * If the WRITE fails, raises ``OSError`` with the counter UNCHANGED in
      memory and on disk. The in-memory value is published only after the
      atomic replace succeeds, so a token can never be minted with a
      generation that is not durable — an unpersisted generation would be
      reloaded lower after restart, letting that token outlive a later
      successful logout. The caller reports the failed logout instead of a
      false success.

    The lock is held through persistence so a concurrent reader never observes
    a generation that might still fail to land; the write is a few bytes and
    callers already run off the event loop for this path.
    """
    global _gen
    # circular import: see _load_revocation_gen_or_none.
    from kiro_crew.config.loader import config_dir

    with _gen_lock:
        base = _gen if _gen is not None else _load_revocation_gen_or_none()
        if base is None:
            raise OSError(
                "cannot read persisted token revocation counter; refusing to "
                "bump from an assumed base"
            )
        new_gen = base + 1
        try:
            p = config_dir() / _REVOCATION_FILE
            p.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace via a same-directory temporary file: a truncate-
            # then-write torn by process termination would leave an empty file
            # that a later boot must treat as unreadable — with the atomic
            # rename, the file on disk always carries either the old or the
            # new complete value.
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(str(new_gen), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            logger.warning("could not persist token revocation counter", exc_info=True)
            raise
        _gen = new_gen
        return new_gen
