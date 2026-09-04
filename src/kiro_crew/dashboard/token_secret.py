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

import errno
import logging
import os
import threading
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir

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

#: Errors that mean the FILESYSTEM has no hard links, as opposed to a link that
#: failed for a reason retrying could fix. Only these may send the publish onto
#: the in-place fallback, because that fallback creates the destination empty and
#: so carries the truncation window this module exists to close: a transient
#: refusal must never be mistaken for a missing filesystem feature. POSIX
#: specifies EPERM for "the filesystem does not support links"; the *NOTSUP pair
#: and ENOSYS are the explicit spellings.
#:
#: Deliberately EXCLUDED: EACCES (a Windows sharing violation while another
#: handle holds the path -- transient, and the one this most needs to keep out),
#: ENOSPC, EDQUOT, EIO and EROFS. Each of those exhausts the retry budget and
#: degrades to an ephemeral secret with the destination untouched. EXDEV is
#: excluded too, for the opposite reason: the staged file is a same-directory
#: sibling, so a cross-device link cannot arise and listing it would only suggest
#: the staging path is allowed to move.
_LINK_UNSUPPORTED_ERRNOS = frozenset(
    {
        getattr(errno, name)
        for name in ("EPERM", "EOPNOTSUPP", "ENOTSUP", "ENOSYS")
        if hasattr(errno, name)
    }
)

#: Windows reports a hard link on FAT/exFAT through a Win32 code whose errno
#: translation is not one of the POSIX names above, so match it directly:
#: ERROR_INVALID_FUNCTION (1) and ERROR_NOT_SUPPORTED (50).
_LINK_UNSUPPORTED_WINERRORS = frozenset({1, 50})


def _is_link_unsupported(exc: OSError) -> bool:
    """Whether *exc* says this filesystem cannot hard-link at all.

    False for anything a retry might clear, which keeps a transient failure from
    routing the key through the in-place create -- see
    :data:`_LINK_UNSUPPORTED_ERRNOS` for why that distinction is the whole point.
    """
    if exc.errno in _LINK_UNSUPPORTED_ERRNOS:
        return True
    return getattr(exc, "winerror", None) in _LINK_UNSUPPORTED_WINERRORS


def _enforce_owner_only(key_path: Path) -> None:
    """Best-effort restrict *key_path* to owner-only (0600 / owner DACL).

    Fail-soft: a read-only FS / chmod failure warns (logging only the PATH,
    never the key bytes) rather than crashing, matching the pre-existing
    behaviour — the secret still signs tokens this session.
    """
    try:
        # restrict_to_owner (fail-loud), NOT chmod_safe: chmod_safe swallows
        # OSError, which would make this security-warning handler dead code.
        # POSIX applies ``chmod 0o600``; Windows applies an owner-only DACL.
        platform_compat.restrict_to_owner(key_path)
    except OSError:
        # Logs the key file PATH (key_path), never the key bytes.
        logger.warning(  # nosemgrep: python-logger-credential-disclosure
            "failed to enforce owner-only permissions on token signing key %s; "
            "file may be readable by other users",
            key_path,
            exc_info=True,
        )


def _unlink_quietly(path: Path) -> None:
    """Remove *path* if present, swallowing any error.

    Used only for THIS process's private staging file, which no other writer
    can name, so there is no identity check to make and no failure worth
    propagating -- a leftover staging file is inert (the key is published under
    its own name) and must never mask the outcome of the publish itself.
    """
    try:
        os.unlink(path)
    except OSError:
        pass


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


def _create_key_in_place(key_path: Path) -> bytes | None:
    """Create the key by exclusive create AT *key_path*, the historical path.

    Reached only when the filesystem cannot hard-link, so the stage-then-link
    publish in :func:`_load_or_create_secret` is unavailable. Returns the new
    key, or ``None`` when another process won the create (the caller retries).

    This carries the pre-existing truncation window with it: the destination is
    created EMPTY and only then written, so a kill between the two leaves a
    0-byte key. That is deliberate -- on a filesystem with no hard links the
    alternative is no persisted key at all -- and it is why the linked publish
    is the default rather than this.
    """
    # O_EXCL guarantees exactly one process across all sharers of this data
    # home wins the create; everyone else hits FileExistsError and loops back
    # to read the winner's bytes. This is what eliminates the divergence: only
    # one key is ever generated.
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
        return None
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
        return None

    # We hold the exclusive create. Capture the created file's identity
    # (device + inode) NOW, while we still hold the fd, so that if we
    # later have to remove a half-written file we unlink ONLY the exact
    # file this process created — never a valid key a racing sibling may
    # have substituted at the same path in the meantime.
    created_stat = os.fstat(fd)
    wrote_durable_key = False
    # Lock the DACL down BEFORE writing the secret bytes: on Windows the
    # lockdown replaces an existing DACL rather than being applied at
    # create time, so writing first would leave a window during which
    # another local principal could slurp the bytes; on POSIX it
    # collapses to an in-process chmod. Create empty
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

    The fix: only ONE key may ever be created, and it is only ever PUBLISHED
    whole. The fresh key is staged into a private sibling file (the shared
    :func:`kiro_crew.atomic_write.atomic_write` helper -- owner-only before the
    first byte, fsynced, cleaned up on failure) and then linked into place with
    ``os.link``. The link is atomic and non-clobbering: exactly one process
    wins, and every other process takes the ``FileExistsError`` branch and READS
    the winner's bytes instead of generating its own.

    ``os.link`` rather than ``os.replace`` is what keeps the single-creator
    election -- a last-writer-wins rename would let each racer install its own
    key, leaving the losers signing with bytes no longer on disk. Staging rather
    than creating in place is what keeps the destination name from ever
    resolving to a partial file: creating it empty and writing afterwards means
    a kill in between (an update completing during shutdown, a reboot) persists
    a 0-byte key, and because the fast-path read then sees <32 bytes forever,
    every later boot degrades to an ephemeral secret -- a dashboard that loads
    while every signed action fails, which a restart cannot fix.

    A small bounded retry loop still covers the publish-then-read interleaving.
    """
    # Local import: config.loader pulls in modules that import token_auth
    # (which re-exports this module), so a top-level import here risks a
    # circular import. Matches the other config_dir() call sites in the
    # dashboard auth modules.
    from kiro_crew.config.loader import config_dir

    try:
        key_path = config_dir() / _SECRET_KEY_FILE
        key_path.parent.mkdir(parents=True, exist_ok=True)

        # Set only by a link failure that says this filesystem HAS no hard
        # links. It is what unlocks the in-place fallback after the loop, so a
        # transient failure leaves it False and never reaches code that creates
        # the destination empty.
        link_unsupported = False

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

            # 2) Publish a WHOLE key, or lose the race and read the winner's.
            #    The key is staged into a private sibling first and only then
            #    linked into place, because the destination name must never
            #    exist in a partial state. An interrupted update -- or a reboot
            #    that kills this process between an in-place create and the
            #    write -- otherwise leaves a 0-byte key on disk, and every
            #    later boot then reads <32 bytes, exhausts the retry budget and
            #    degrades to an ephemeral secret: a dashboard that loads while
            #    every signed action fails, which a restart cannot fix because
            #    it re-reads the same empty file.
            #
            #    os.link is the publish step, not os.replace, because exactly
            #    ONE key may ever exist. link fails with FileExistsError once a
            #    sibling has published, which keeps the single-creator election
            #    the in-place O_EXCL create used to provide; os.replace would
            #    let each racer install its own key and leave the losers
            #    signing with bytes that are no longer on disk.
            # The suffix is load-bearing, not cosmetic. This file holds the FULL
            # key until the publish link lands, and a kill between that link and
            # the cleanup unlink below leaves it on disk. security.py's keystone
            # fence protects a publish artifact by SHAPE -- a direct child of a
            # keystone leaf's own directory whose name ends in one of
            # _KEYSTONE_ARTIFACT_SUFFIXES -- so ".tmp" is what puts a leftover
            # copy of the signing key behind the same fence as the key itself,
            # rather than leaving it readable to agent tools (a forged-token
            # path). It also matches the suffix atomic_write's own mkstemp temp
            # already uses. test_token_auth.py pins this against the real
            # predicate so a rename cannot silently leave the fence behind.
            staged = key_path.with_name(
                f".{key_path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
            )
            key = os.urandom(_MIN_KEY_BYTES)
            try:
                # restrict_to_owner (rather than mode=0o600 alone) is what
                # _enforce_owner_only applies today, and the helper applies it
                # BEFORE any key byte reaches the file, so the secret never
                # exists under a wider mode. restrict_on_error="warn" preserves
                # this call site's fail-soft permission policy, and fsync=True
                # carries the os.fsync() the in-place writer performed.
                atomic_write(
                    staged,
                    key,
                    fsync=True,
                    restrict_to_owner=True,
                    restrict_on_error="warn",
                )
            except OSError:
                # Staging failed. No destination file was ever created, so
                # key_path is untouched. On Windows this can be a TRANSIENT
                # sharing violation, so retry within the budget rather than
                # degrading immediately; a genuinely unwritable directory
                # exhausts the loop and lands on the fallback below.
                _unlink_quietly(staged)
                logger.debug(  # nosemgrep: python-logger-credential-disclosure -- logs the path only, never key bytes
                    "token signing key staging failed at %s; retrying", key_path
                )
                time.sleep(_CREATE_BACKOFF_SECONDS)
                continue
            try:
                os.link(staged, key_path)
            except FileExistsError:
                # A sibling published first. Drop our candidate and loop back to
                # read theirs; never install it over the winner.
                _unlink_quietly(staged)
                time.sleep(_CREATE_BACKOFF_SECONDS)
                continue
            except OSError as exc:
                # Retry either way -- a transient sharing violation (Windows,
                # while another handle holds the destination) succeeds on a
                # later attempt, and a filesystem with no hard links simply
                # fails every attempt. What differs is what the exhausted budget
                # is allowed to do next: ONLY an unsupported-link error may fall
                # back to creating the destination in place, because that path
                # reintroduces the truncation window. A transient error must
                # degrade to an ephemeral secret with key_path untouched.
                if _is_link_unsupported(exc):
                    link_unsupported = True
                _unlink_quietly(staged)
                logger.debug(  # nosemgrep: python-logger-credential-disclosure -- logs the path only, never key bytes
                    "token signing key publish link failed at %s (errno=%s); retrying",
                    key_path,
                    exc.errno,
                )
                time.sleep(_CREATE_BACKOFF_SECONDS)
                continue
            # Linked: the name now resolves to the fully-written inode. The key
            # BYTES were fsynced during staging, but the new NAME lives in the
            # parent directory, so the entry is not durable until that directory
            # is synced -- and the next statement would remove the only other
            # name pointing at the inode.
            #
            # Strict, NOT best_effort: best_effort downgrades even EIO to a
            # warning, which is right for a caller whose work is already
            # committed and wrong here, where a swallowed failure followed by the
            # unlink can leave a power loss with neither name. Then the inode is
            # unreachable, the next boot mints a fresh key, and every outstanding
            # cookie and link signed by the old one is invalid.
            #
            # A real failure is caught rather than propagated: raising would hit
            # the outer handler and hand back an EPHEMERAL secret while a valid
            # key sits at key_path, which is the divergence this function exists
            # to prevent. So keep the recoverable second name and return the key
            # that IS on disk. Keeping it is only safe because the staging name
            # ends in ".tmp": the leftover stays behind security.py's keystone
            # fence instead of becoming a readable copy of the signing key.
            #
            # fsync_dir returns quietly where a directory sync cannot be
            # EXPRESSED (Windows has no directory descriptor; some network mounts
            # reject it) and raises only where the device refused the write, so
            # the normal and unsupported paths still reach the unlink below.
            try:
                fsync_dir(key_path.parent)
            except OSError:
                logger.warning(  # nosemgrep: python-logger-credential-disclosure -- logs the path only, never key bytes
                    "could not sync %s after publishing the token signing key; "
                    "keeping the staging copy as a recoverable second name",
                    key_path.parent,
                    exc_info=True,
                )
            else:
                _unlink_quietly(staged)
            _enforce_owner_only(key_path)
            return key

        # Retries exhausted. The in-place fallback below creates the
        # destination EMPTY and writes afterwards, so it carries the very
        # truncation window this fix removes -- it is worth that only where the
        # alternative is no persisted key on any boot, i.e. a filesystem that
        # genuinely cannot hard-link (FAT/exFAT, some network mounts). Anything
        # else that exhausted the budget -- a transient sharing violation, a
        # policy refusal, a short/empty file already at key_path -- skips it and
        # degrades to the ephemeral secret with key_path untouched, exactly as
        # the pre-PR code did.
        #
        # Fall back under its OWN bounded loop.
        #
        # The loop is what makes this converge, and a single attempt would not.
        # Two gateways booting concurrently on a link-less home both exhaust the
        # publish loop above and reach here; O_EXCL lets exactly one create the
        # key, and the LOSER gets None back. Without the re-read below it would
        # fall through to an ephemeral secret while the winner persisted a real
        # one, leaving the pair unable to validate each other's tokens -- the
        # divergence the exclusive create exists to prevent. So the loser loops,
        # reads the winner's bytes, and returns those instead.
        if link_unsupported:
            for _attempt in range(_CREATE_MAX_ATTEMPTS):
                try:
                    existing = key_path.read_bytes()
                except FileNotFoundError:
                    existing = b""
                except OSError:
                    # Windows sharing violation while the winner holds the file
                    # open; transient, so retry rather than degrade.
                    time.sleep(_CREATE_BACKOFF_SECONDS)
                    continue
                if len(existing) >= _MIN_KEY_BYTES:
                    _enforce_owner_only(key_path)
                    return existing
                created = _create_key_in_place(key_path)
                if created is not None:
                    return created
                # Lost the create (or it failed): give the winner a beat, then
                # loop back and read what they persisted.
                time.sleep(_CREATE_BACKOFF_SECONDS)

        # A persistently short/empty file (external corruption, or a creator
        # that was killed mid-publish on a filesystem with no hard links). Do
        # NOT truncate-and-regenerate — that reintroduces the exact divergence
        # race this function exists to prevent. Degrade to an ephemeral secret
        # (works this session, not across restart), matching the
        # unwritable-file fallback below. An operator can remove the stale file
        # to let a fresh key be created cleanly.
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
