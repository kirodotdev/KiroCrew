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
import stat
import threading
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir

logger = logging.getLogger(__name__)

_SECRET_KEY_FILE = "token_signing.key"
_MIN_KEY_BYTES = 32

#: Directory (a direct child of the crew data home) both gateway auth stores stage their
#: temp files in: this module's ``token_signing.key`` and ``refresh_tokens``'
#: ``refresh_chains.json``. Spelled as a literal rather than imported from
#: ``kiro_crew.sandbox`` to keep this module off that import chain, exactly as
#: ``service/live_target.py`` spells ``live-target-staging``; the equality is pinned by a
#: test instead.
#:
#: Staging beside either leaf is what this exists to avoid. Both are masked in an agent
#: sandbox as individual FILES, and a mask covers a PATH rather than an inode, so a temp
#: beside one of them sits in the data-home root -- visible in every sandbox, and
#: ``link(2)``-able by a same-uid agent for as long as the write takes. The temp carries
#: the FULL key or chain state, so a link taken in that window keeps reading the bytes
#: after the publish, and a crash between write and publish leaves the same unmasked file
#: on disk. The spawn-time hard-link refusal cannot close this: it runs BEFORE a spawn,
#: while this window opens during one.
#:
#: The sandbox launcher masks this directory in every namespace and precreates it, and
#: ``security.paths`` fences it, both as a whole DIRECTORY -- so every temp name inside it,
#: present and future, is covered without a second entry to remember.
_AUTH_STORE_STAGING_LEAF = "auth-store-staging"

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
#: excluded too, for the opposite reason: the staged file lives in a SUBDIRECTORY of the
#: key's own parent (see :data:`_AUTH_STORE_STAGING_LEAF`), so it and the destination are
#: always on one filesystem and a cross-device link cannot arise. Listing it would only
#: suggest the staging path is allowed to leave that filesystem.
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


class AuthStoreStagingRefused(OSError):
    """The staging directory is REFUSED because another account could write it.

    Raised for the two permission refusals only -- a POSIX mode that keeps a group or world
    write bit, and a Windows directory that could not be locked to its owner. A home that
    simply cannot stage (a non-directory occupying the name) raises a plain ``OSError``,
    because the two need opposite answers.

    An ``OSError`` subclass so every existing caller keeps its handler: the refresh store
    marks itself degraded on either kind and does not need to tell them apart.

    The signing key does. Its publish loop treats a plain failure as "this home cannot
    stage", which unlocks :func:`_create_key_in_place` -- and that writer creates the
    destination EMPTY before writing, which is the truncation window the staged publish
    exists to remove. Buying that window is worth it where no key would be persisted
    otherwise. It is not worth it here: the refusal is a one-``chmod`` misconfiguration this
    module reports in full, and an in-place publish answers nothing about it, so the key
    degrades to an ephemeral secret with the stored file untouched rather than leaving a host
    that published atomically yesterday with a zero-byte key today.
    """


def _is_link_unsupported(exc: OSError) -> bool:
    """Whether *exc* says this filesystem cannot hard-link at all.

    False for anything a retry might clear, which keeps a transient failure from
    routing the key through the in-place create -- see
    :data:`_LINK_UNSUPPORTED_ERRNOS` for why that distinction is the whole point.
    """
    if exc.errno in _LINK_UNSUPPORTED_ERRNOS:
        return True
    return getattr(exc, "winerror", None) in _LINK_UNSUPPORTED_WINERRORS


def auth_store_staging_dir(home: Path) -> Path:
    """Return the auth-store staging directory under *home*, creating it if absent.

    Shared by this module's signing-key publish and ``refresh_tokens``' state publish so
    the two cannot drift onto different staging policies. The directory is validated
    rather than trusted, on the same grounds ``service.live_target._publish_pointer``
    validates its own: the name sits in the data-home root, so a directory found there is
    not necessarily one this process created.

    Refuses the two shapes that defeat the mask, and repairs the one that does not:

    * not a directory (a symlink included, via ``lstat``) -- REFUSED. Following it would
      stage the full key or chain state wherever it points, outside both the mask and the
      fence, which is the exposure this directory exists to close.
    * cannot be locked to its owner ON WINDOWS -- REFUSED. There
      :func:`platform_compat.restrict_dir_to_owner` writes an inheriting owner-only DACL,
      which no POSIX mode test can express, so a directory a foreign principal can write
      would otherwise pass unexamined and that principal could replace a staged file
      between the write and the publish.
    * group- or world-WRITABLE on POSIX where the narrowing does not stick -- REFUSED.
      Another local account can replace the staged file between ``atomic_write`` and the
      publish ``os.link``, which installs a signing key it chose. Nothing downstream would
      notice: the publishing process returns its own bytes while every later boot reads the
      substituted file as authoritative. A mount that cannot express modes can still
      hard-link, so "the mode did not stick" does not imply the publish will fail.
    * group- or world-READABLE or executable on POSIX -- narrowed, and WARNED when the
      narrowing does not stick. A wider read bit lets another local account enumerate the
      temps' names, so it is worth closing, but it moves no byte and grants no
      substitution. This is the ``dir_mode=0755`` class of mount (CIFS, vfat, exFAT),
      which is a real configuration rather than a hostile one.

    Refusing drops no persisted record, because each caller reaching a refusal answers it
    rather than returning silently. Both answers are DEGRADATIONS that leave the stored file
    untouched, raised as :class:`AuthStoreStagingRefused` so a caller can tell a refusal from
    a home that merely could not be staged in. The signing key degrades to an ephemeral
    secret for this process, which costs a re-login after a restart and keeps the persisted
    key byte-for-byte as it was -- it does NOT take the in-place writer, whose empty-create
    would trade a one-``chmod`` misconfiguration for a zero-byte key on a host that was
    publishing atomically. The refresh store marks itself degraded, and its own reader fails
    closed on that mark, so a spent or revoked token is REFUSED rather than accepted -- it
    does not publish a full copy of the chain state through the state file's own
    sandbox-visible directory, which is the exposure this staging directory exists to close.

    ``EXDEV`` is not a concern for the caller that publishes with ``os.link``: this is a
    subdirectory of the key's own parent, so the staged file and its destination are
    always on one filesystem.
    """
    staging = home / _AUTH_STORE_STAGING_LEAF
    try:
        st = os.lstat(staging)
    except FileNotFoundError:
        home.mkdir(parents=True, exist_ok=True)
        # exist_ok: the sandbox materialiser precreates the same directory, and the two
        # auth stores publish concurrently.
        staging.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(staging)
    if not stat.S_ISDIR(st.st_mode):
        # NOT AuthStoreStagingRefused: this says the home cannot stage at all, not that
        # another account could substitute the staged file. The key's publish loop is meant to
        # reach its in-place writer here, because the alternative is no persisted key on any
        # boot while a working path to one exists.
        raise OSError(
            errno.ENOTDIR,
            f"{staging} is not a directory; refusing to stage a gateway auth store through it",
        )
    # Owner-only on BOTH platforms, through the directory-shaped helper. Its Windows
    # arm writes an inheriting owner-only DACL, which no mode test can express, so a
    # POSIX-only check would leave a Windows staging directory a foreign principal can
    # write -- and that principal could then replace a staged file between the write
    # and the publish. Its POSIX arm is the same owner-only narrowing the mode split
    # below measures. The helper is fail-loud, and on Windows it writes only when the
    # directory's own descriptor does not already match, so the ordinary publish pays
    # nothing.
    try:
        platform_compat.restrict_dir_to_owner(staging)
    except OSError as exc:
        if os.name == "nt":
            raise AuthStoreStagingRefused(
                errno.EPERM,
                f"{staging} could not be locked to its owner ({exc}); refusing to stage a "
                "gateway auth store through it. Another account that can write this "
                "directory can replace the staged file between the write and the publish "
                "link, which installs a signing key or refresh state it chose. Grant only "
                "this account access to it, or remove it, then restart the gateway.",
            ) from exc
        # POSIX falls through: the mode test below can SEE what survived, and it
        # separates the bit that permits substitution from the bit that only leaks
        # names. Refusing on the read bit too would fail an ordinary mount.
    else:
        st = os.lstat(staging)
    if os.name != "nt" and st.st_mode & 0o022:
        raise AuthStoreStagingRefused(
            errno.EPERM,
            f"{staging} is group- or world-WRITABLE (mode {stat.S_IMODE(st.st_mode):o}) and "
            "the mode could not be narrowed; refusing to stage a gateway auth store through "
            "it. Another local account can replace the staged file between the write and the "
            "publish link, which installs a signing key it chose as the one that signs every "
            "dashboard token. chmod 700 it or remove it, then restart the gateway.",
        )
    if os.name != "nt" and st.st_mode & 0o055:
        logger.warning(
            "gateway auth-store staging directory %s is readable beyond its owner (mode %o) "
            "and could not be narrowed; other local accounts can list the publish temps' "
            "names. The bytes stay owner-only and the directory is still masked in every "
            "agent namespace and fenced from the file tools. chmod 700 it to close the "
            "listing.",
            staging,
            stat.S_IMODE(st.st_mode),
        )
    return staging


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
    """Create the key by exclusive create AT *key_path*, the in-place fallback.

    Reached only when the filesystem cannot hard-link, so the stage-then-link
    publish in :func:`_load_or_create_secret` is unavailable. Returns the new
    key, or ``None`` when another process won the create (the caller retries).

    This path carries a truncation window: the destination is created EMPTY and
    only then written, so a kill between the two leaves a 0-byte key. That is
    deliberate -- on a filesystem with no hard links the alternative is no
    persisted key at all -- and it is why the linked publish is the default
    rather than this.
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
        # Set when the staging directory itself is unusable for the whole budget. Its only
        # job is to reach the same in-place fallback: that writer needs no staging
        # directory, so a validator refusal must not be the reason no key is ever
        # persisted. Kept separate from link_unsupported so neither flag claims a
        # filesystem property the other observed.
        staging_unusable = False

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
                # retry budget is exhausted (~1s later).
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
            #    an in-place O_EXCL create provides; os.replace would
            #    let each racer install its own key and leave the losers
            #    signing with bytes that are no longer on disk.
            # Staged in a masked, fenced DIRECTORY rather than beside the key.
            # This file holds the FULL key until the publish link lands, and a
            # kill between that link and the cleanup unlink below leaves it on
            # disk. Beside the key it sat in the data-home root, which is
            # sandbox-visible and same-uid writable, so a leftover -- or the
            # write window itself -- was a readable copy of the signing key and
            # a link(2) target for an agent: a forged-token path.
            #
            # _AUTH_STORE_STAGING_LEAF is masked in every agent namespace
            # (sandbox._CREW_HIDDEN_LEAVES) and fenced from the file tools
            # (security.paths._CREW_SECRET_LEAVES), in both cases as a whole
            # directory, so every name inside it is covered however it is
            # spelled. The keystone-artifact suffix rule does not reach this
            # path: it covers a direct child of a keystone leaf's own directory,
            # and this file sits one level below that.
            #
            # The ".tmp" suffix matches the shape atomic_write's own mkstemp temp
            # uses. What makes THIS file unreachable is the directory holding it,
            # not its name: the keystone-artifact suffix rule belongs to
            # security.paths, which gates the agent's FILE TOOLS, so it does not
            # stop a shell inside the namespace from opening a path it can see.
            # test_token_auth.py pins the staged path against the real predicate
            # so a rename cannot silently leave the fence behind.
            #
            # Resolved inside the retry budget: this validator can fail for a
            # transient reason, and a failure that escaped the loop would skip
            # the in-place fallback below and leave no persisted key at all.
            key = os.urandom(_MIN_KEY_BYTES)
            try:
                staging_dir = auth_store_staging_dir(key_path.parent)
            except AuthStoreStagingRefused:
                # A POLICY refusal, not a home that cannot stage. It will not clear inside
                # the retry budget -- an operator clears it with one chmod, and the refusal
                # message says which -- and the in-place writer answers none of it: that
                # writer creates key_path EMPTY and fills it afterwards, so unlocking it here
                # would trade a misconfigured directory mode for a zero-byte signing key on a
                # host that published atomically until now. Leave both flags as they are and
                # break: the loop's exit degrades to the ephemeral secret with the persisted
                # key untouched, which is what the policy below and this function's own
                # comment promise for a refusal.
                logger.warning(  # nosemgrep: python-logger-credential-disclosure -- logs the path and the refusal reason only, never key bytes
                    "gateway auth-store staging directory refused for %s; the signing key is "
                    "NOT re-published and this process uses an ephemeral secret. The "
                    "persisted key is unchanged. Fix the directory and restart.",
                    key_path,
                    exc_info=True,
                )
                break
            except OSError:
                # Nothing was created, so there is no candidate to remove. A failure that
                # persists for the whole budget must still reach the in-place fallback:
                # _create_key_in_place writes key_path directly and needs no staging
                # directory, so the alternative is an ephemeral secret on every boot while a
                # working path existed.
                staging_unusable = True
                logger.debug(  # nosemgrep: python-logger-credential-disclosure -- logs the path only, never key bytes
                    "token signing key staging directory unusable for %s; retrying",
                    key_path,
                )
                time.sleep(_CREATE_BACKOFF_SECONDS)
                continue
            staged = staging_dir / (f".{key_path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp")
            # This attempt HAS a staging directory, so an earlier attempt's lookup failure was
            # transient and must not still be speaking for the loop. The flag decides whether
            # the in-place fallback -- which creates the destination empty and so carries the
            # truncation window this function exists to remove -- is allowed at all, and the
            # policy below admits it only for a home that genuinely cannot stage. Leaving a
            # stale True here would widen that to any home that faltered once and then
            # recovered, which is the opposite of what the surrounding comment promises.
            staging_unusable = False
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
            # lives inside _AUTH_STORE_STAGING_LEAF: that directory is masked in
            # every agent namespace and fenced from the file tools, so the
            # leftover stays unreachable instead of becoming a readable copy of
            # the signing key.
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

        # Retries exhausted, or a policy refusal broke out of them. The in-place fallback
        # below creates the
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
        if link_unsupported or staging_unusable:
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
