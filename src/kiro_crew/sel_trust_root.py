"""The SEL trust root: the HMAC key that signs the audit chain, and the MAC it computes.

Where the key lives (``<log dir>/trust/sel_hmac.key``), how it is created atomically,
validated before it ever signs, and migrated in from the legacy location beside the
log; and the per-record HMAC every writer stamps and every reader checks.

Each function here whose first parameter is ``self`` is a ``SecurityEventLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller. Module
names this code reads are patched through ``kiro_crew.sel``, which reaches every member
of the family that binds them (``kiro_crew._facade_mirror``).

Instance fields written: ``_hmac_key_file``.

Instance fields read: ``_dir``, ``_hmac_key``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.owner_only_files import _is_link_unsupported, mkdirs_owner_only

if TYPE_CHECKING:
    from kiro_crew.sel import SecurityEvent, SecurityEventLog

#: The SEL's own logger: this code logs under ``kiro_crew.sel``.
logger = logging.getLogger("kiro_crew.sel")


_HMAC_KEY_FILE = "sel_hmac.key"


# Dedicated trust-root subdirectory (owner-only, 0o700) holding the HMAC key.
# The key must not live NEXT TO the log it signs: an actor with write access to
# the log directory could otherwise read the key, rewrite security_events.jsonl,
# and re-sign a clean-looking chain that verify_integrity() accepts. A legacy
# key at ``<config_dir>/sel_hmac.key`` is migrated in atomically (same bytes, so
# every existing chain still verifies) — see _load_or_create_hmac_key.
_TRUST_SUBDIR = "trust"


# Minimum accepted HMAC key length. os.urandom(32) is always written, so a
# shorter key on disk means truncation/corruption/tampering — signing the
# audit chain with an empty or short key yields a predictable, forgeable MAC
# and silently disables the chain's tamper-evidence. Mirrors the >= 32-byte
# requirement enforced in dashboard/token_secret.py.
_HMAC_KEY_MIN_BYTES = 32


def _load_or_create_hmac_key(self: SecurityEventLog) -> bytes:
    trust_dir = self._dir / _TRUST_SUBDIR
    key_path = trust_dir / _HMAC_KEY_FILE
    legacy_path = self._dir / _HMAC_KEY_FILE
    self._dir.mkdir(parents=True, exist_ok=True)
    # The upgrade boundary is hostile ground: BEFORE this release, ``trust``
    # was not on the sensitive-path deny list, so an agent could have
    # pre-planted a ``trust`` symlink/junction (pointing the key write
    # somewhere it can read) or a ``trust/sel_hmac.key`` with bytes it
    # knows — letting it forge SEL chain and session-identity MACs after
    # the upgrade. Two defenses, both BEFORE anything trusts the
    # destination:
    #   1. a linked ``trust`` entry is removed (link only, never its
    #      target) so the real directory is created in its place;
    #   2. when a genuine legacy key exists, it WINS over any
    #      pre-existing destination file (see the migration block below) —
    #      the legacy key is the only one that was deny-list-protected all
    #      along, so it is the only trustworthy chain anchor here.
    if platform_compat.is_link_or_junction(trust_dir):
        logger.warning(
            "SEL trust dir %s is a symlink/junction — removing the link "
            "(planted before upgrade?) and creating a real directory",
            trust_dir,
        )
        try:
            platform_compat.unlink_link_or_junction(trust_dir)
        except OSError:
            # Read-only config dir: the link cannot be removed. NEVER use
            # the linked destination — fall back to the legacy key when one
            # exists (same fail-soft as an uncreatable trust dir below);
            # a fresh install with an unremovable planted link cannot
            # proceed safely, so surface the failure.
            if legacy_path.exists():
                logger.warning(
                    "cannot remove linked SEL trust dir %s; continuing with "
                    "the legacy key location",
                    trust_dir,
                    exc_info=True,
                )
                key_path = legacy_path
            else:
                raise
    # Owner-only trust dir. The creation mode is ignored when the dir
    # already exists, so chmod_safe re-asserts 0o700 every init
    # (fail-soft: a read-only FS must not take down SecurityEventLog init;
    # Windows relies on the key FILE's owner-only DACL below). Creation
    # failure itself is fail-soft too: a legacy install on a read-only
    # config dir must keep signing with its existing key at the legacy
    # location, not crash before the migration fallback can run.
    if key_path != legacy_path:
        try:
            mkdirs_owner_only(trust_dir)
        except OSError:
            if legacy_path.exists():
                logger.warning(
                    "cannot create SEL trust dir %s; continuing with the legacy key location",
                    trust_dir,
                    exc_info=True,
                )
                key_path = legacy_path
            else:
                # Fresh install on an unwritable config dir: key creation
                # below would fail anyway — surface the real cause.
                raise
        else:
            platform_compat.chmod_safe(trust_dir, 0o700)
    # A linked destination KEY file is removed for the same reason as a
    # linked dir above (the link is removed, never its target): a fresh
    # key must never be written THROUGH a planted link, and a read must
    # never follow one.
    if key_path != legacy_path and platform_compat.is_link_or_junction(key_path):
        logger.warning(
            "SEL HMAC key path %s is a symlink/junction — removing the link "
            "(planted before upgrade?)",
            key_path,
        )
        try:
            platform_compat.unlink_link_or_junction(key_path)
        except OSError:
            # Same fail-soft as the linked dir above: never use the linked
            # destination; prefer the legacy key, else surface the failure.
            if legacy_path.exists():
                logger.warning(
                    "cannot remove linked SEL HMAC key %s; continuing with "
                    "the legacy key location",
                    key_path,
                    exc_info=True,
                )
                key_path = legacy_path
            else:
                raise
    # ── Migration: relocate a legacy key sitting next to the log ──
    # os.replace is atomic on the same filesystem and preserves the key
    # BYTES, so every HMAC chain already written to security_events.jsonl
    # still verifies — no re-signing. The >= length validation and
    # restrict_to_owner below apply to the migrated file exactly as to a
    # pre-existing one at the new path. Skipped when trust-dir creation
    # above already fell back to the legacy location.
    if key_path != legacy_path and legacy_path.exists():
        # ── The one shape this migration refuses to resolve on its own ──
        # A legacy file too short to BE a key, sitting beside a destination
        # that loads as one, is AMBIGUOUS, and neither half of the ambiguity
        # may be acted on:
        #
        # * Promoting the short legacy file destroys the only copy of the key
        #   that signed every existing record, and the >= length check below
        #   then fails init on every later boot, so the loss is unrecoverable.
        #   A mixed-binary window produces exactly this shape benignly -- a
        #   writer deriving its chain lock from the key location opens it with
        #   ``O_CREAT`` and leaves a 0-byte file behind once a sibling has
        #   relocated the real key.
        # * ADOPTING the destination is no safer, and this is the half that
        #   cannot be repaired by measuring harder. ``_trust_root_key_loads``
        #   is a stat -- regular file, >= the minimum length -- and length is
        #   not trust: an agent holding the sandbox's read-write leaves can
        #   truncate the legacy key below the minimum AND plant a long-enough
        #   destination whose bytes it knows. Every source that could tell the
        #   planted key from the real one -- the destination itself, the
        #   legacy key, ``security_events.jsonl`` and ``security_events.d``
        #   -- is read-WRITE for that same agent (``sandbox_leaves.py``'s
        #   ``_CREW_SANDBOX_VISIBLE_LEAVES``), so a check against any of them
        #   authenticates the attacker's own evidence. Making the audit log
        #   the anchor is the worst of those, because a forged newest row
        #   turns a fail-closed check into adoption of a key the attacker
        #   knows, and every SEL and session-identity MAC signed afterwards is
        #   forgeable with no recovery path.
        #
        # With no operator-protected source to authenticate against, the only
        # correct move is to do NOTHING and refuse: nothing is adopted,
        # nothing is overwritten, both files survive for the operator, and an
        # install that reached this state by the benign route is recovered by
        # hand rather than by a guess that a planted key satisfies equally.
        # Every other pairing falls through to the unconditional rule below,
        # exactly as it did before this guard existed.
        if not _trust_root_key_loads(legacy_path) and _trust_root_key_loads(key_path):
            raise RuntimeError(
                f"legacy SEL HMAC key {legacy_path} is too short to be a key "
                f"while a loadable key sits at {key_path}. This install cannot "
                "tell a half-finished key relocation from a planted key, because "
                "every file that could prove the difference is writable by the "
                "audited agent. Refusing to promote the short legacy key (that "
                "would destroy the key signing the existing chain) and refusing "
                "to adopt the key at the destination. Restore the correct key "
                f"from backup, or remove both {legacy_path} and {key_path} to "
                "start a fresh chain with a new key."
            )
        else:
            # The legacy key WINS over any pre-existing destination file.
            # ``trust/`` was not deny-listed before this release, so a file
            # already at the destination on a legacy install is untrustworthy
            # (an agent could have planted bytes it knows and then forged SEL
            # and session-identity MACs); the legacy key is the only anchor
            # that was deny-list-protected all along. os.replace overwrites
            # the destination atomically. Benign overlap (a backup restore
            # resurrecting the legacy file after a real migration) is
            # unaffected: the key never rotates, so the bytes are identical.
            if key_path.exists():
                logger.warning(
                    "pre-existing file at %s is being replaced by the legacy SEL "
                    "HMAC key %s (the legacy key is the deny-list-protected "
                    "trust anchor)",
                    key_path,
                    legacy_path,
                )
            try:
                os.replace(legacy_path, key_path)
                logger.info("migrated SEL HMAC key %s -> %s", legacy_path, key_path)
            except OSError:
                # Ordering is security-relevant: while the legacy source STILL
                # EXISTS it stays the only deny-list-protected trust anchor, so
                # a failed replace must fall back to it, never to a
                # destination file that could have been pre-planted (an
                # attacker able to make os.replace fail must not get their
                # planted key adopted). The destination is trusted only after
                # the legacy source is gone, which on a failed replace can only
                # mean a sibling process completed the same migration (its
                # os.replace moved the SAME legacy bytes there).
                if legacy_path.exists():
                    # Chain continuity beats relocation: if the move fails
                    # (read-only FS, permissions), keep signing with the
                    # legacy file rather than minting a fresh key that would
                    # orphan every already-chained record. Path stays legacy
                    # for this process so sel_hmac_key_path() reports the
                    # file in use.
                    logger.warning(
                        "failed to migrate SEL HMAC key %s -> %s; continuing with "
                        "the legacy location",
                        legacy_path,
                        key_path,
                        exc_info=True,
                    )
                    key_path = legacy_path
                elif key_path.exists():
                    # Lost the migration race to a sibling process: the key
                    # is already at the new path, and its bytes are the same
                    # legacy bytes, so proceed with it.
                    logger.debug(
                        "SEL HMAC key migration raced; using already-migrated %s",
                        key_path,
                    )
                # else: both paths vanished mid-init (external deletion):
                # fall through to fresh-key creation at the NEW path.
    # Single source of truth for dependent protocols: sel_hmac_key_path()
    # reports THIS resolved path (normally trust/sel_hmac.key; the legacy
    # path only on a failed migration above).
    self._hmac_key_file = key_path
    if key_path == legacy_path:
        # The legacy location is the data-home root, which no mask covers as a
        # directory, so a staging name there would sit outside every fence.
        # Publish there by atomic replace only, with no second name.
        return _load_or_replace_hmac_key(key_path)
    # Runs on every init at the trust location, whether it loads or creates.
    _sweep_dead_staging_copies(key_path)
    # Bounded: a lost publish race goes round once more to load the winner's
    # key, and a transient link refusal is retried.
    for _attempt in range(_PUBLISH_MAX_ATTEMPTS):
        if key_path.exists():
            return _load_existing_hmac_key(key_path)
        key = os.urandom(32)
        if _publish_new_hmac_key(key_path, key):
            return key
    # The budget went on link refusals or a name that kept vanishing: load or
    # publish by atomic replace, so init never fails where a replace succeeds.
    return _load_or_replace_hmac_key(key_path)


#: Load-or-publish attempts before the atomic-replace publish. A lost race needs two.
_PUBLISH_MAX_ATTEMPTS = 5

#: Whether ``os.link`` can be told not to follow a symlink at its source.
_LINK_NOFOLLOW = os.link in os.supports_follow_symlinks

#: Staging names are ``.<key name>.<pid>.<16 hex>.tmp``.
_STAGING_SUFFIX = ".tmp"
_STAGING_HEX_DIGITS = frozenset("0123456789abcdef")


def _load_or_replace_hmac_key(key_path: Path) -> bytes:
    """Load the key at *key_path*, or publish a fresh one by atomic replace.

    The replace is last-writer-wins, so two first starts on this path can mint two
    keys. It is the publish for the legacy location, a filesystem that cannot
    hard-link, and an exhausted link budget.
    """
    if key_path.exists():
        return _load_existing_hmac_key(key_path)
    key = os.urandom(32)
    # ``restrict_to_owner=True`` locks the temp file down before the key bytes
    # reach it and implies 0o600 on POSIX; ``restrict_on_error="warn"`` keeps init
    # fail-soft on a read-only FS (see test_chmod_failure_is_swallowed).
    atomic_write(key_path, key, restrict_to_owner=True, restrict_on_error="warn")
    return key


def _staging_pid(name: str, key_name: str) -> int | None:
    """The pid in a staging name for *key_name*, or None for any other name."""
    prefix = f".{key_name}."
    if not (name.startswith(prefix) and name.endswith(_STAGING_SUFFIX)):
        return None
    parts = name[len(prefix) : -len(_STAGING_SUFFIX)].split(".")
    if len(parts) != 2:
        return None
    pid, token = parts
    if not (pid.isascii() and pid.isdigit()) or len(token) != 16:
        return None
    if not set(token) <= _STAGING_HEX_DIGITS:
        return None
    return int(pid)


def _sweep_dead_staging_copies(key_path: Path) -> None:
    """Remove staging copies of the key left by processes that have exited.

    A process killed between staging and its cleanup leaves a full key copy under
    its staging name. Only regular files whose name is a staging name for this key
    and whose pid is reported DEAD are removed; a live, unsignalable or unknown pid
    keeps its file, and every other entry is left alone.
    """
    try:
        with os.scandir(key_path.parent) as entries:
            candidates = [e for e in entries if _staging_pid(e.name, key_path.name) is not None]
    except OSError:
        return
    for entry in candidates:
        pid = _staging_pid(entry.name, key_path.name)
        if pid is None or pid == os.getpid():
            continue
        if platform_compat.pid_liveness(pid) != platform_compat.PID_DEAD:
            continue
        try:
            if entry.is_file(follow_symlinks=False):
                os.unlink(entry.path)
        except OSError:
            logger.debug("could not remove stale SEL HMAC key staging file %s", entry.path)


def _reassert_owner_only(key_path: Path) -> None:
    """Re-lock *key_path* owner-only, warning (path only) when that fails.

    Fail-SOFT (warn, don't crash): a read-only FS / chmod failure must not take
    down SecurityEventLog init.
    """
    try:
        # Covers an inode this module did not lock down itself: a migrated legacy
        # key or a restored file, whose mode may be wide.
        platform_compat.restrict_to_owner(key_path)  # lockdown-ok: load-time re-assert
    except OSError:
        # Logs the key file PATH, never the key bytes.
        logger.warning(  # nosemgrep: python-logger-credential-disclosure
            "SECURITY: failed to enforce owner-only permissions on SEL HMAC key %s; "
            "file may be readable by other users",
            key_path,
            exc_info=True,
        )


def _load_existing_hmac_key(key_path: Path) -> bytes:
    """Read, validate and re-lock the key already at *key_path*."""
    # On Windows a reader that opens the key while a winner unlinks its staging
    # name (the same inode) can lose a sharing window; the helper retries that
    # PermissionError with backoff on Windows only and re-raises it at once on POSIX.
    existing = read_bytes_with_retry(key_path)
    # Validate the key BEFORE it ever signs the chain. A
    # 0-byte or too-short key is accepted silently by hmac.new(),
    # producing a predictable, forgeable MAC that disables the audit
    # chain's tamper-evidence. Fail HARD (mirroring token_secret.py's
    # >= 32-byte requirement) rather than silently falling back to a
    # weak key. We RAISE instead of regenerating (unlike
    # token_secret.py's fall-through) because a fresh key would orphan
    # every already-chained record — an operator must consciously
    # rotate/restore the key.
    if len(existing) < _HMAC_KEY_MIN_BYTES:
        raise RuntimeError(
            f"SEL HMAC key {key_path} is too short ({len(existing)} bytes; "
            f"require >= {_HMAC_KEY_MIN_BYTES}). Refusing to sign the audit "
            "chain with a weak/forgeable key. Restore the correct key from "
            "backup, or remove the file to start a fresh chain with a new key."
        )
    # Re-enforce owner-only perms at LOAD time, not just at creation:
    # the mode may have been relaxed since (backup restore, manual edit,
    # migration) and this key signs the entire audit chain — a
    # group/world-readable key lets any local user forge valid MACs.
    # Mirrors token_secret.py's load-time restrict_to_owner.
    _reassert_owner_only(key_path)
    return existing


def _publish_new_hmac_key(key_path: Path, key: bytes) -> bool:
    """Publish *key* at *key_path* only if no key is there yet.

    Returns False when another process published first (the caller then loads
    that process's key, so every process that starts together signs with ONE
    key) or when the link was refused for a reason a retry may clear.
    """
    # The key is written WHOLE to a private staging name beside the destination
    # and then hard-linked into place. ``os.link`` never replaces an existing
    # name, so when two first starts race exactly one link lands and the other
    # gets FileExistsError. A temp + ``os.replace`` publish is last-writer-wins:
    # each racer installs its own key and the loser keeps signing with bytes no
    # longer on disk, so every record it writes fails verify. This is the
    # publish ``dashboard/token_secret.py`` uses for its signing key.
    #
    # The staging file sits in the key's own directory (``trust/``), so it has
    # the key's own sandbox and file-tool disposition, and a link cannot cross
    # devices. Linking shares the inode, so the destination name only ever
    # resolves to a complete key that is already owner-only.
    staged = key_path.parent / f".{key_path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    # Create the staging copy ATOMICALLY (temp file + rename via ``atomic_write``,
    # whose short-write loop was modelled on the hand-rolled one this call
    # replaces): a plain os.open()+os.write() is NOT atomic — a crash or
    # full-disk partial write leaves a 0-byte/short key on disk, which the
    # load-time length check above would then reject with a hard
    # RuntimeError on the NEXT boot, bricking every SecurityEventLog()
    # init until an operator manually removes the file. ``fsync=True`` makes
    # the bytes durable before a name that other processes trust points at them.
    #
    # ``restrict_to_owner=True`` locks the temp file down BEFORE the key
    # bytes reach it — a post-rename lockdown would leave a brand-new
    # key readable under the inherited DACL on Windows for the write
    # window — and implies 0o600 on POSIX.
    # ``restrict_on_error="warn"`` keeps this site's fail-SOFT policy: a
    # read-only FS / chmod failure must not crash SecurityEventLog init
    # (see test_chmod_failure_is_swallowed). The linked-parent refusal
    # implied by ``restrict_to_owner=True`` raises unconditionally, which
    # is the right behavior for the key that signs the audit chain: a
    # pre-planted link under the trust dir is hostile.
    atomic_write(staged, key, fsync=True, restrict_to_owner=True, restrict_on_error="warn")
    try:
        # No-follow pins the link to the inode this process just wrote, even if
        # the staging name is swapped for a symlink after the write.
        if _LINK_NOFOLLOW:
            os.link(staged, key_path, follow_symlinks=False)
        else:
            os.link(staged, key_path)
    except FileExistsError:
        try:
            st = os.lstat(key_path)
        except OSError:
            return False  # gone again; the next pass reads or publishes
        if stat.S_ISREG(st.st_mode):
            # Another process published first; never install ours over it.
            return False
        # The name is held by something that is not a key file (a dangling or
        # looping link): publish by atomic replace, which replaces that entry,
        # rather than retrying into a failure.
        atomic_write(key_path, key, restrict_to_owner=True, restrict_on_error="warn")
    except OSError as exc:
        logger.debug(
            "SEL HMAC key link publish failed at %s (errno=%s)",
            key_path,
            exc.errno,
            exc_info=True,
        )
        if not _is_link_unsupported(exc):
            return False  # transient: retried inside the caller's budget
        # This filesystem cannot hard-link (FAT/exFAT, some network mounts).
        # Publish by atomic replace instead, so such a home still gets a
        # persisted key. shortcut: the two-key race stays open on such a
        # filesystem only; upgrade if one becomes a supported home.
        if key_path.exists():
            return False
        atomic_write(key_path, key, restrict_to_owner=True, restrict_on_error="warn")
    finally:
        # The staging name is ours alone; a leftover is inert, never a key in use,
        # and _sweep_dead_staging_copies removes one a killed process left behind.
        try:
            os.unlink(staged)
        except OSError:
            pass
    # A creation lockdown failure is logged under the staging name; name the key.
    _reassert_owner_only(key_path)
    return True


def _compute_hash(self: SecurityEventLog, event: SecurityEvent) -> str:
    # Hash over all fields except entry_hash itself
    d = asdict(event)
    d.pop("entry_hash", None)
    payload = json.dumps(d, sort_keys=True).encode()
    return hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()


def _record_signature_matches(self: SecurityEventLog, record: dict) -> bool:
    """Whether *record*'s ``entry_hash`` is our HMAC over its other fields."""
    data = dict(record)
    stored = data.pop("entry_hash", "")
    if not isinstance(stored, str) or not stored:
        return False
    try:
        payload = json.dumps(data, sort_keys=True).encode()
    except (TypeError, ValueError):
        return False
    expected = hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(stored, expected)


def _trust_root_key_loads(path: Path) -> bool:
    """True when *path* is a regular file currently holding a usable key.

    ``stat`` only — the caller reads the bytes just after, and the point here is
    to answer "does the resolved path still resolve?" without paying a read on
    the healthy path. ``S_ISREG`` is load-bearing rather than tidiness: a
    candidate location an actor can create is a place they can put a FIFO, and
    opening one blocks in-kernel forever, which an ``asyncio`` timeout cannot
    reclaim because the thread is stuck in a syscall.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size >= _HMAC_KEY_MIN_BYTES
