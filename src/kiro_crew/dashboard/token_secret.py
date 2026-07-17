"""Persistent HMAC signing secret for dashboard auth tokens.

Extracted from ``token_auth.py`` so both the token-auth module and the
refresh-token module can import the secret without creating a circular
dependency between them. The secret is shared across both modules.

The secret is stored at ``<config_dir>/token_signing.key`` (owner-only
0600). Persistence is required for correctness: tokens and session
cookies are HMAC-signed with this key, so a fresh random secret on every
process start would invalidate every outstanding Slack link and cookie,
locking users out after any gateway restart.

Loading is LAZY (``_get_secret()``), NOT a module-level call: merely
*importing* this module must not write ``token_signing.key`` into
``$KIROCREW_HOME``. The CLI imports token_auth (and thus this module)
transitively for every ``kirocrew`` subcommand, so an import-time write
(a) breaks ``gateway --seed`` — which requires an empty target home and
refuses a non-empty one — and (b) pollutes the home for read-only
commands like ``kirocrew --help``.
"""

from __future__ import annotations

import logging
import os
import threading

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

_SECRET_KEY_FILE = "token_signing.key"


def _load_or_create_secret() -> bytes:
    """Return the HMAC signing secret, persisted across restarts.

    See module docstring for the persistence rationale. Falls back to an
    ephemeral secret if the key file is unwritable — tokens still work
    within this process; they just won't survive a restart (the
    pre-existing behaviour).
    """
    # Local import: config.loader pulls in modules that import token_auth
    # (which re-exports this module), so a top-level import here risks a
    # circular import. Matches the other config_dir() call sites in the
    # dashboard auth modules.
    from kiro_crew.config.loader import config_dir

    try:
        key_path = config_dir() / _SECRET_KEY_FILE
        if key_path.exists():
            existing = key_path.read_bytes()
            if len(existing) >= 32:
                # Re-enforce 0600 at load time, not just at creation: perms may
                # have been relaxed since (backup restore, manual edit, migration)
                # and this key signs all auth tokens/cookies.
                try:
                    # restrict_to_owner (fail-loud), NOT chmod_safe: chmod_safe
                    # swallows OSError, which would make this security-warning
                    # handler dead code (AutoSDE). POSIX applies ``chmod 0o600``;
                    # Windows applies an owner-only DACL via icacls — no NTFS
                    # posture regression from the earlier IS_POSIX no-op, which
                    # left the key signing all auth tokens/cookies world-readable.
                    platform_compat.restrict_to_owner(key_path)
                except OSError:
                    # Logs the key file PATH (key_path), never the key bytes.
                    logger.warning(  # nosemgrep: python-logger-credential-disclosure
                        "failed to enforce owner-only permissions on token signing key %s; "
                        "file may be readable by other users",
                        key_path,
                        exc_info=True,
                    )
                return existing
        key = os.urandom(32)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        # Lock the DACL down BEFORE writing the secret bytes: on Windows
        # restrict_to_owner shells out to icacls (subprocess), which is a
        # measurable window (dozens to hundreds of ms). If we wrote the secret
        # first, another local principal that can enumerate ~/.kirocrew could
        # slurp it during that window. Create empty → tighten DACL → write.
        # On POSIX the equivalent gap collapses to an in-process os.chmod
        # syscall, but the same order is fine.
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            try:
                platform_compat.restrict_to_owner(key_path)
            except OSError:
                # Security-sensitive: this key signs all auth tokens/cookies,
                # so a world-readable key file is a real exposure. Warn loudly
                # rather than failing — the secret still works for signing this
                # session, and the caller falls back to the ephemeral path only
                # on unwritable file, not on chmod failure.
                # Logs the key file PATH (key_path), never the key bytes.
                logger.warning(  # nosemgrep: python-logger-credential-disclosure
                    "failed to set owner-only permissions on token signing key %s; "
                    "file may be readable by other users",
                    key_path,
                    exc_info=True,
                )
            os.write(fd, key)
        finally:
            os.close(fd)
        return key
    except OSError:
        # Fall back to an ephemeral secret if the key file is unwritable.
        logger.warning("token signing key not persisted; using ephemeral secret", exc_info=True)
        return os.urandom(32)


_SECRET: bytes | None = None
_SECRET_LOCK = threading.Lock()


def _get_secret() -> bytes:
    """Return the HMAC signing secret, loading/creating it on first use.

    Lazy (NOT a module-level call) so that merely *importing* this module
    does not write ``token_signing.key`` into ``$KIROCREW_HOME`` — see the
    module docstring. Memoized under a lock so the key is loaded exactly
    once even under concurrent first use.
    """
    global _SECRET
    if _SECRET is None:
        with _SECRET_LOCK:
            if _SECRET is None:
                _SECRET = _load_or_create_secret()
    return _SECRET
