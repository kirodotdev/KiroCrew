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
import time
from pathlib import Path

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

_SECRET_KEY_FILE = "token_signing.key"
_MIN_KEY_BYTES = 32

# Bounded retry budget for the create-then-read interleaving window. The SOLE
# creator opens the key file with O_EXCL, then writes 32 bytes; a racing reader
# that opened the file in that sliver observes it empty/short and must retry to
# see the winner's bytes. The window is a single os.write(), so a few dozen
# short sleeps (~1s total worst case) is comfortably enough without any risk of
# an unbounded hang.
_CREATE_MAX_ATTEMPTS = 50
_CREATE_BACKOFF_SECONDS = 0.02


def _enforce_owner_only(key_path: Path) -> None:
    """Best-effort restrict *key_path* to owner-only (0600 / owner DACL).

    Fail-soft: a read-only FS / chmod failure warns (logging only the PATH,
    never the key bytes) rather than crashing, matching the pre-existing
    behaviour — the secret still signs tokens this session.
    """
    try:
        # restrict_to_owner (fail-loud), NOT chmod_safe: chmod_safe swallows
        # OSError, which would make this security-warning handler dead code.
        # POSIX applies ``chmod 0o600``; Windows applies an owner-only DACL via
        # icacls.
        platform_compat.restrict_to_owner(key_path)
    except OSError:
        # Logs the key file PATH (key_path), never the key bytes.
        logger.warning(  # nosemgrep: python-logger-credential-disclosure
            "failed to enforce owner-only permissions on token signing key %s; "
            "file may be readable by other users",
            key_path,
            exc_info=True,
        )


def _unlink_if_same_file(key_path: Path, created_stat: os.stat_result) -> None:
    """Remove *key_path* only if it is still the exact file identified by
    *created_stat* (matching ``st_dev`` + ``st_ino``).

    Used to clean up a half-written key file that THIS process created via the
    exclusive-create path but then failed to fully persist. Two safety
    properties:

    * We compare against ``os.lstat`` (which does NOT follow symlinks) so an
      attacker cannot get us to traverse a symlink swapped in at the path.
    * The device+inode identity match means we never delete a *valid* key that
      a racing sibling has since created at the same path — only the exact
      incomplete file we opened. If identity differs (or the file is already
      gone), we leave the path untouched.

    Best-effort: an unlink failure is logged (path only, never key bytes) and
    swallowed so it never masks the original write failure — the next boot's
    retry loop still degrades safely to an ephemeral secret.
    """
    try:
        on_disk = os.lstat(key_path)
    except OSError:
        # Already gone (a sibling cleaned it up, or it never landed) — nothing
        # of ours to remove.
        return
    if (
        on_disk.st_dev == created_stat.st_dev
        and on_disk.st_ino == created_stat.st_ino
    ):
        try:
            os.unlink(key_path)
        except OSError:
            # Logs the key file PATH (key_path), never the key bytes.
            logger.warning(  # nosemgrep: python-logger-credential-disclosure
                "could not remove incomplete token signing key %s after a "
                "failed create; a stale short key file may remain and should "
                "be deleted manually",
                key_path,
                exc_info=True,
            )


def _load_or_create_secret() -> bytes:
    """Return the HMAC signing secret, persisted across restarts.

    See module docstring for the persistence rationale. Falls back to an
    ephemeral secret if the key file is unwritable — tokens still work within
    this process; they just won't survive a restart (the pre-existing
    behaviour).

    Cross-process atomicity of key *creation* is the crux here. Multiple
    first-time gateways sharing one data home (``warm_auth_singletons()`` warms
    this before the port bind, so two racing boots can both observe the key as
    absent) MUST converge on a SINGLE key. A plain last-writer-wins write —
    even an atomic temp-file + rename — is NOT sufficient: each process would
    generate its own key, one file would survive on disk, and the loser would
    keep an in-memory key that no longer matches the persisted file, silently
    corrupting every token it issues for sibling instances / after a restart.

    The fix: only ONE key may ever be created. We attempt an exclusive create
    (``O_CREAT | O_EXCL``); exactly one process wins and writes the fresh key,
    and every other process takes the ``FileExistsError`` branch and READS the
    winner's bytes instead of generating its own. A small bounded retry loop
    covers the create-then-read interleaving (a racing reader can momentarily
    see the just-created, not-yet-written empty file).
    """
    # Local import: config.loader pulls in modules that import token_auth
    # (which re-exports this module), so a top-level import here risks a
    # circular import. Matches the other config_dir() call sites in the
    # dashboard auth modules.
    from kiro_crew.config.loader import config_dir

    try:
        key_path = config_dir() / _SECRET_KEY_FILE
        key_path.parent.mkdir(parents=True, exist_ok=True)

        for _attempt in range(_CREATE_MAX_ATTEMPTS):
            # 1) Fast path: an already-populated key file. Read the persisted
            #    bytes VERBATIM and never regenerate, so a restart or a sibling
            #    process signs with the identical key.
            try:
                existing = key_path.read_bytes()
            except FileNotFoundError:
                existing = b""
            except OSError:
                # A concurrent creator can hold the file open while it writes the
                # fresh key; on Windows that read raises a sharing violation
                # (PermissionError / WinError 32). This is TRANSIENT — back off and
                # retry within the bounded loop rather than letting it propagate to
                # the outer ephemeral fallback, which would make this racer diverge
                # from the winner's persisted key (silent auth corruption). POSIX
                # permits the concurrent read, so this branch is Windows-only; a
                # genuinely unreadable file still degrades to ephemeral after the
                # retry budget is exhausted (same as before, ~1s later).
                logger.debug(  # nosemgrep: python-logger-credential-disclosure -- logs the path only, never key bytes
                    "token signing key read contended at %s; retrying", key_path
                )
                time.sleep(_CREATE_BACKOFF_SECONDS)
                continue
            if len(existing) >= _MIN_KEY_BYTES:
                # Re-enforce 0600 at load time, not just at creation: perms may
                # have been relaxed since (backup restore, manual edit,
                # migration) and this key signs all auth tokens/cookies.
                _enforce_owner_only(key_path)
                return existing

            # 2) Try to become the SOLE creator. O_EXCL guarantees exactly one
            #    process across all sharers of this data home wins the create;
            #    everyone else hits FileExistsError and loops back to read the
            #    winner's bytes. This is what eliminates the divergence: only
            #    one key is ever generated.
            try:
                # os.O_BINARY is REQUIRED on Windows: os.open() there defaults
                # to TEXT mode, so the os.write() below would translate any
                # 0x0A ('\n') byte in the random key to 0x0D 0x0A ('\r\n'),
                # persisting a longer, corrupted key that the creator's
                # in-memory bytes (and every sibling read) no longer match ->
                # silent auth divergence. getattr(..., 0) makes it a no-op on
                # POSIX, where os.O_BINARY does not exist and there is no text
                # mode. Evaluated at call time so tests can simulate the flag.
                fd = os.open(
                    str(key_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except FileExistsError:
                # Someone else created it (possibly not yet written). Give the
                # writer a beat, then loop back to read their bytes.
                time.sleep(_CREATE_BACKOFF_SECONDS)
                continue
            except OSError:
                # On Windows the winner may hold the freshly-created file open
                # while it writes; a racer's exclusive-create can then hit a
                # sharing violation (PermissionError / WinError 32) INSTEAD of
                # FileExistsError. Treat it as contention — back off and retry
                # within the bounded loop rather than propagating to the outer
                # ephemeral fallback, which would make this racer diverge from
                # the winner's persisted key (silent auth corruption). POSIX does
                # not raise here; a genuinely unwritable dir still degrades to an
                # ephemeral secret once the retry budget is exhausted.
                logger.debug(  # nosemgrep: python-logger-credential-disclosure -- logs the path only, never key bytes
                    "token signing key exclusive-create contended at %s; retrying",
                    key_path,
                )
                time.sleep(_CREATE_BACKOFF_SECONDS)
                continue

            # We hold the exclusive create. Capture the created file's identity
            # (device + inode) NOW, while we still hold the fd, so that if we
            # later have to remove a half-written file we unlink ONLY the exact
            # file this process created — never a valid key a racing sibling may
            # have substituted at the same path in the meantime.
            created_stat = os.fstat(fd)
            wrote_durable_key = False
            # Lock the DACL down BEFORE writing the secret bytes: on Windows
            # restrict_to_owner shells out to icacls (a measurable subprocess
            # window during which another local principal could slurp the
            # bytes); on POSIX it collapses to an in-process chmod. Create empty
            # → tighten → write → fsync.
            try:
                _enforce_owner_only(key_path)
                key = os.urandom(_MIN_KEY_BYTES)
                # os.write() may return a SHORT count (notably on a nearly-full
                # disk). Loop until every byte lands; a 0-byte write is an error.
                mv = memoryview(key)
                while mv:
                    n = os.write(fd, mv)
                    if n == 0:
                        raise OSError(
                            "short write persisting token signing key (wrote 0 bytes)"
                        )
                    mv = mv[n:]
                # Cross-restart persistence is the entire reason this file
                # exists, so flush the bytes to stable storage before we treat
                # the key as durable and hand it back. A failing fsync means the
                # key is not reliably persisted — fall into the cleanup path.
                os.fsync(fd)
                wrote_durable_key = True
            finally:
                os.close(fd)
                if not wrote_durable_key:
                    # The exclusive create succeeded but we failed to persist a
                    # full, durable key (ENOSPC, quota, fsync failure, ...). The
                    # on-disk file is now short/empty; leaving it PERMANENTLY
                    # poisons every future boot — the fast-path read sees
                    # <32 bytes, the O_EXCL create then hits FileExistsError, the
                    # retry budget exhausts, and every gateway falls back to a
                    # fresh ephemeral key (tokens die on each restart, concurrent
                    # gateways cannot validate one another) until a human deletes
                    # it. Remove OUR incomplete file so the next init can create a
                    # valid key cleanly. The identity guard ensures we never
                    # delete a valid key a sibling has since substituted.
                    _unlink_if_same_file(key_path, created_stat)
            # Reaching here means the write + fsync completed; a failure would
            # have propagated the OSError to the outer handler (the ephemeral
            # fallback) after the cleanup above ran.
            return key

        # Retries exhausted: a persistently short/empty file (external
        # corruption, or a creator that crashed after O_EXCL but before the
        # write). Do NOT truncate-and-regenerate — that reintroduces the exact
        # divergence race this function exists to prevent. Degrade to an
        # ephemeral secret (works this session, not across restart), matching
        # the unwritable-file fallback below. An operator can remove the stale
        # file to let a fresh key be created cleanly.
        # Logs only the key PATH (key_path) and an attempt count, never the key
        # bytes; the Semgrep rule fires on the credential-adjacent wording in
        # the static message string, not on any secret value.
        logger.warning(  # nosemgrep: python-logger-credential-disclosure
            "token signing key at %s did not converge to a valid persisted "
            "key after %d attempts; using ephemeral secret",
            key_path,
            _CREATE_MAX_ATTEMPTS,
        )
        return os.urandom(_MIN_KEY_BYTES)
    except OSError:
        # Fall back to an ephemeral secret if the key file is unwritable.
        logger.warning("token signing key not persisted; using ephemeral secret", exc_info=True)
        return os.urandom(_MIN_KEY_BYTES)


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
