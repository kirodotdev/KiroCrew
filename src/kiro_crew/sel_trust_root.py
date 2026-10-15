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
from kiro_crew.atomic_write import atomic_write

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
    # Owner-only trust dir. mkdir's mode is umask-filtered and ignored when
    # the dir already exists, so chmod_safe re-asserts 0o700 every init
    # (fail-soft: a read-only FS must not take down SecurityEventLog init;
    # Windows relies on the key FILE's owner-only DACL below). Creation
    # failure itself is fail-soft too: a legacy install on a read-only
    # config dir must keep signing with its existing key at the legacy
    # location, not crash before the migration fallback can run.
    if key_path != legacy_path:
        try:
            trust_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    if key_path.exists():
        existing = key_path.read_bytes()
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
        # Mirrors token_secret.py's load-time restrict_to_owner. Fail-SOFT
        # at the call site (warn, don't crash): a read-only FS / chmod
        # failure must not take down SecurityEventLog init.
        try:
            # The only preceding write to key_path is the legacy-key migration rename,
            # in a mutually exclusive branch.
            platform_compat.restrict_to_owner(key_path)  # lockdown-ok: load-time re-assert
        except OSError:
            # Logs the key file PATH, never the key bytes.
            logger.warning(  # nosemgrep: python-logger-credential-disclosure
                "failed to enforce owner-only permissions on SEL HMAC key %s; "
                "file may be readable by other users",
                key_path,
                exc_info=True,
            )
        return existing
    key = os.urandom(32)
    # Create the key ATOMICALLY (temp file + rename via ``atomic_write``,
    # whose short-write loop was modelled on the hand-rolled one this call
    # replaces): a plain os.open()+os.write() is NOT atomic — a crash or
    # full-disk partial write leaves a 0-byte/short key on disk, which the
    # load-time length check above would then reject with a hard
    # RuntimeError on the NEXT boot, bricking every SecurityEventLog()
    # init until an operator manually removes the file. os.replace() makes
    # the key file visible only once it is complete.
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
    atomic_write(key_path, key, restrict_to_owner=True, restrict_on_error="warn")
    return key


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
