"""One-time importer that moves plaintext Jira ``.env`` tokens into the vault.

Kiro Crew stored the Jira API token as a plaintext ``KEY=VALUE`` line in the
data home's ``.env`` (``~/.kiro/crew/.env``). The encrypted vault
(:class:`~kiro_crew.secrets.SecretVault`) supersedes that store, and the
``secret://`` resolver / vault-aware Jira consumer reads the secret without
ever exposing the plaintext to the agent.

This module bridges the two for the ONLY vault-aware credential surface in this
change: the global ``JIRA_API_TOKEN`` and the per-host ``JIRA_TOKEN_<HEX>``
tokens that ``_get_jira_auth`` resolves vault-first. For each such token still
held in plaintext it writes the value into the vault under the same key name
and rewrites the ``.env`` line to ``KEY=secret://KEY``. Other credential keys
(Slack, Discord, kiro-cli, …) are deliberately NOT migrated — their consumers
still read the literal ``.env`` value, so rewriting them to a ``secret://``
reference would break auth. The source file is never deleted — the caller is
told exactly what migrated and how to remove the leftover plaintext.

The importer is idempotent and order-correct: a line already holding a
``secret://`` reference (or a later empty line) for a key WINS over any earlier
plaintext line for that key, so re-running after an apply never regresses an
already-migrated key. A key already present in the vault is treated as
authoritative — its plaintext ``.env`` value is NEVER written over the stored
secret (only the ``.env`` line is rewritten to the ``secret://`` reference) —
but ONLY when its stored entry actually decrypts; a listed-but-undecryptable
entry (missing/mismatched vault key) aborts the migration so the still-usable
plaintext is not rewritten away. A key whose value is overridden by a nonempty
process-environment entry is skipped (the environment is runtime-authoritative,
so migrating the stale file value would shadow the live override). The ``.env``
read-snapshot → compare → rewrite runs as one critical section under an
exclusive OS advisory lock (``platform_compat.try_acquire_lock`` on a sidecar
``.env.lock``; POSIX flock / Windows msvcrt), with two complementary guards: the
lock serializes against any OTHER PROCESS that holds the same lock (a second
importer, or any writer that adopts it — today also the WeChat/Weixin QR
sign-in handler in ``dashboard.handlers.weixin_qr``), and — under that lock — a
compare-and-swap re-read aborts the rewrite (no write) if the bytes changed since
the snapshot, catching any residual in-process writer that might not take the
lock.  If the lock cannot be taken, the migration aborts cleanly and a re-run
reconciles (idempotent, secret://-line-wins).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import _JIRA_TOKEN_RE, CRED_JIRA_API_TOKEN, config_dir, env_path
from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX
from kiro_crew.secrets import SecretVault

logger = logging.getLogger(__name__)

#: ``secret://`` scheme prefix a migrated ``.env`` value is rewritten to.
#: Imported from the resolver (single source of truth) so the writer here and
#: the reader there can never drift.
_SECRET_URI_PREFIX = SECRET_URI_PREFIX


def _env_lock_path(ep: Path) -> Path:
    """Return the sidecar lock-file path for a given ``.env`` path.

    Every ``.env`` writer that needs cross-process exclusion MUST derive the
    lock path via this helper so that all writers provably share the same
    advisory lock file.

    The lock file lives inside the ``.vault`` directory (a sibling of ``.env``
    in the config dir) rather than next to ``.env`` itself.  Every sandbox tier
    bind-mount-hides the whole ``.vault`` tree, so an unconfined agent cannot
    delete or replace the held lock inode to defeat serialisation — a lock
    outside ``.vault`` is in the config dir, which the agent can write freely.

    The parent ``.vault`` directory is created on demand (mode 0o700) so the
    lock is valid even before the vault is first used.

    Used by:
      * :func:`migrate_env_secrets` — the CLI importer
      * :func:`kiro_crew.dashboard.handlers.weixin_qr._commit_credential_and_config`
        — the QR sign-in handler (the only other in-tree ``.env`` writer)
      * :func:`kiro_crew.dashboard.handlers.messaging._write_env_updates`
        — the dashboard channel-credential writer
    """
    vault_dir = ep.parent / ".vault"
    vault_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return vault_dir / ".env.lock"


class MigrationConflictError(RuntimeError):
    """Raised when the ``.env`` changed under a concurrent writer mid-migration.

    The rewrite is computed from a snapshot read at the start; if the file
    differs at write time the rewrite is aborted (no write) so a token another
    writer added is never clobbered.
    """


def _is_recognized_key(key: str) -> bool:
    """True if *key* is a Jira credential the vault-aware consumer reads.

    Scoped deliberately to the ONLY credential surface that resolves through
    the vault in this change: the global ``JIRA_API_TOKEN`` and the per-host
    ``JIRA_TOKEN_<HEX>`` tokens that ``_get_jira_auth`` reads vault-first.

    Other ``_CREDENTIAL_KEYS`` (Slack, Discord, kiro-cli, …) are intentionally
    NOT migrated: their consumers still read the literal ``.env`` value, so
    rewriting their line to a ``secret://`` reference would hand them the URI
    string instead of the secret and break auth. They migrate once their
    consumers become vault-aware in a later change.
    """
    return key == CRED_JIRA_API_TOKEN or bool(_JIRA_TOKEN_RE.match(key))


@dataclass
class MigrationReport:
    """Outcome of a migration pass.

    ``migrated`` names the keys moved into the vault (empty on a dry run —
    nothing is written; the dry-run pass instead populates it with the keys it
    *would* migrate). ``already_referenced`` names keys whose ``.env`` value was
    already a ``secret://`` reference (skipped as a no-op). ``dry_run`` records
    whether this pass wrote anything. ``orphaned_vault_keys`` names any keys
    whose rollback-delete raised an error during a failed apply — those vault
    entries may still exist and could shadow future rotations; manual cleanup
    under Settings > Secrets in the dashboard is recommended.
    """

    env_file: Path
    dry_run: bool
    migrated: list[str] = field(default_factory=list)
    already_referenced: list[str] = field(default_factory=list)
    orphaned_vault_keys: list[str] = field(default_factory=list)


def _parse_env_lines(text: str) -> list[tuple[str, str]]:
    """Return ``(key, value)`` for every ``KEY=VALUE`` line in *text*.

    Matches :meth:`KiroCrewConfig.load_credentials` parsing: strips whitespace,
    ignores blanks and ``#`` comments, splits on the first ``=``. Last write of
    a repeated key wins there; here every occurrence is returned so the rewrite
    can address each physical line.
    """
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            k, v = stripped.split("=", 1)
            pairs.append((k.strip(), v.strip()))
    return pairs


def _rewrite_env_text(text: str, migrated_keys: set[str]) -> str:
    """Return *text* with each ``migrated_keys`` line rewritten to a ref.

    Preserves comments, blank lines, ordering and any unrecognized keys.
    Only ``KEY=VALUE`` lines whose key is in *migrated_keys* are rewritten to
    ``KEY=secret://KEY``; everything else is emitted verbatim — including each
    line's ORIGINAL terminator (``\\r\\n``/``\\n``/``\\r`` or none on the last
    line), so a CRLF ``.env`` is not silently converted to LF.
    """
    out: list[str] = []
    for raw in text.splitlines(keepends=True):
        # Split the physical line into (content, terminator) so a rewritten line
        # keeps the exact terminator the original had.
        content = raw
        term = ""
        for end in ("\r\n", "\n", "\r"):
            if raw.endswith(end):
                content = raw[: -len(end)]
                term = end
                break
        stripped = content.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in migrated_keys:
                out.append(f"{key}={_SECRET_URI_PREFIX}{key}{term}")
                continue
        out.append(raw)
    return "".join(out)


def migrate_env_secrets(
    *,
    dry_run: bool = True,
) -> MigrationReport:
    """Migrate recognized plaintext ``.env`` credentials into the vault.

    Always reads the data home's ``.env`` (:func:`env_path`) AND writes to the
    data home's own vault (:func:`config_dir`) — there is deliberately no
    caller-supplied path parameter for EITHER, so no code path (and no
    agent-influenced argument) can point the importer at an arbitrary file to
    read, nor at an arbitrary directory to write the vault key/entries into. For
    each line
    whose key is recognized (:data:`_CREDENTIAL_KEYS` or a ``JIRA_TOKEN_<HEX>``)
    and whose value is still plaintext, the value is stored in the vault under
    the same key and the line is queued for rewrite to ``KEY=secret://KEY``.

    When ``dry_run`` is True (the default) nothing is written — the returned
    :class:`MigrationReport` lists what *would* migrate. When False, vault
    writes happen first, then the ``.env`` file is rewritten atomically at mode
    ``0o600``. The source file is never deleted.

    Idempotent: a value already of the form ``secret://…`` is recorded under
    ``already_referenced`` and skipped, so a re-run after an apply is a no-op.

    Blocking file IO and (on apply) blocking vault writes: call via
    ``asyncio.to_thread`` from async paths. Tests point at a temp home by
    monkeypatching :func:`kiro_crew.secrets.migrate.env_path` and
    :func:`kiro_crew.secrets.migrate.config_dir`.
    """
    ep = env_path()
    report = MigrationReport(env_file=ep, dry_run=dry_run)

    try:
        original_bytes = ep.read_bytes()
    except OSError:
        # No .env (or unreadable): nothing to migrate.
        return report
    text = original_bytes.decode("utf-8", errors="surrogateescape")

    vault = SecretVault(config_dir())
    to_migrate: dict[str, str] = {}

    for key, value in _parse_env_lines(text):
        if not _is_recognized_key(key):
            continue
        if value.startswith(_SECRET_URI_PREFIX):
            # An authoritative already-migrated reference. A later secret://
            # line for a key WINS over any earlier plaintext line for the same
            # key: drop the stale plaintext from the queue so --apply never
            # overwrites the good vault entry with a stale value. This is what
            # makes re-running import idempotent — a migrated key is never
            # regressed.
            to_migrate.pop(key, None)
            if key not in report.already_referenced:
                report.already_referenced.append(key)
            continue
        if not value:
            # Present but empty — a later empty line also wins over earlier
            # plaintext (nothing to store, and no stale value should linger in
            # the queue). Leave the line alone.
            to_migrate.pop(key, None)
            continue
        # Runtime override wins — but ONLY for keys that load_credentials
        # actually overlays from the process environment. That overlay is scoped
        # to _CREDENTIAL_KEYS, of which our recognized set contains exactly the
        # GLOBAL `JIRA_API_TOKEN`. The per-host `JIRA_TOKEN_<HEX>` keys are NOT
        # in _CREDENTIAL_KEYS, so the environment does NOT override them at
        # runtime — Jira reads the .env/vault value. Skipping a per-host key just
        # because a same-named env var happens to exist would leave the stale
        # file token unmigrated while Jira keeps using it, so restrict the skip
        # to the global token. For it, a nonempty env value is the effective
        # credential, and migrating the stale .env value would let vault-first
        # resolution shadow the live override.
        if key == CRED_JIRA_API_TOKEN and os.environ.get(key):
            to_migrate.pop(key, None)
            continue
        # Plaintext value: last plaintext write of a repeated key wins (matches
        # load_credentials); a later secret:// / empty line above clears it.
        to_migrate[key] = value

    if not to_migrate:
        return report

    if dry_run:
        report.migrated = list(to_migrate)
        return report

    # A key already present in the vault is AUTHORITATIVE: the vault holds the
    # current secret and `.env` may only carry a stale plaintext copy. Never
    # overwrite it — doing so would replace a valid token with a stale one.
    # Such keys are still rewritten to a ``secret://`` reference below (the
    # vault already holds the value, so pointing `.env` at it is correct and
    # keeps the two consistent / the op idempotent), but they are excluded from
    # the vault writes. A key counts as "already in the vault" (authoritative,
    # skip the write, safe to rewrite its .env line to secret://) ONLY if its
    # stored entry actually DECRYPTS. A listed-but-undecryptable entry means the
    # vault key is missing/mismatched (e.g. a restored store); trusting it and
    # rewriting the .env plaintext to secret://KEY would strand Jira on an
    # unusable vault entry while destroying the still-valid plaintext. So on any
    # undecryptable candidate, abort before writing anything — the operator must
    # repair or remove the corrupt vault entry first.
    listed = set(vault.list_names())
    already_in_vault: set[str] = set()
    for key in to_migrate:
        if key not in listed:
            continue
        try:
            secret = vault.get(key)
        except Exception as exc:
            raise MigrationConflictError(
                f"vault already has an entry for {key!r} that cannot be "
                f"decrypted ({exc.__class__.__name__}); refusing to migrate so "
                f"the usable plaintext in the .env is not lost. Repair or remove "
                f"the vault entry, then re-run `kirocrew secrets import --apply`."
            ) from exc
        if secret is None:
            # Listed a moment ago but gone now — a concurrent delete between
            # list_names() and get(). Treat like a vanished/undecryptable entry:
            # abort rather than rewrite the usable plaintext to a dangling
            # secret://KEY reference.
            raise MigrationConflictError(
                f"vault entry for {key!r} disappeared during migration "
                f"(concurrent change); no rewrite performed. Re-run "
                f"`kirocrew secrets import --apply` to reconcile."
            )
        stored = secret.reveal()
        # The vault entry decrypts, but to a DIFFERENT value than the .env
        # plaintext we are about to rewrite away. That divergence is a real
        # conflict, not an idempotent re-run: e.g. this run stored the .env's
        # `A`, a concurrent edit then rewrote the vault entry to `B`, and blindly
        # rewriting the .env `A` line to `secret://KEY` now discards the `A`
        # plaintext while Jira resolves to `B` — a silent value swap the operator
        # never asked for. (The idempotent case — .env plaintext equals what the
        # vault already holds — falls through and is rewritten to secret://KEY as
        # before.) Abort so the operator picks the authoritative value rather
        # than letting the importer choose one and destroy the other.
        if stored != to_migrate[key]:
            raise MigrationConflictError(
                f"vault already has an entry for {key!r} whose value differs "
                f"from the plaintext in the .env; refusing to migrate so neither "
                f"value is silently discarded. Reconcile them (update the .env to "
                f"match the vault, or overwrite the vault entry under "
                f"Settings > Secrets in the dashboard), then re-run "
                f"`kirocrew secrets import --apply`."
            )
        already_in_vault.add(key)
    to_store = {k: v for k, v in to_migrate.items() if k not in already_in_vault}

    # Serialize the read-snapshot → compare → vault-store → re-verify → rewrite
    # as one critical section under an exclusive OS advisory lock (the mandated
    # cross-platform helper — POSIX flock / Windows msvcrt), taken on a sidecar
    # lock file so it does not perturb the .env's own bytes/mode. Two guards,
    # each covering what the other cannot:
    #   * the LOCK serializes against any OTHER PROCESS touching the .env under
    #     the same lock (a second `secrets import`, or the WeChat/Weixin QR
    #     sign-in handler — see `dashboard.handlers.weixin_qr` — which now also
    #     acquires this same lock before its read-modify-write of .env),
    #     closing the cross-process compare-then-replace window;
    #   * the CAS re-read (below, under the lock) provides defense-in-depth
    #     against any future in-process writer that might not take the file lock,
    #     by aborting if the bytes changed since the snapshot.
    # If the lock cannot be taken (another holder), abort cleanly rather than
    # block the CLI; a re-run reconciles (idempotent, secret://-line-wins).
    #
    # ORDERING INVARIANT: the vault write (`_store_all`) runs INSIDE this
    # critical section, AFTER the CAS confirms `.env` is unchanged from the
    # original snapshot, so the vault never ends up holding a value that `.env`
    # has already moved past (a concurrent `.env` edit between the snapshot and
    # the store now causes abort BEFORE any vault write, closing the
    # "stale vault authoritative after aborted rewrite" window).
    new_text = _rewrite_env_text(text, set(to_migrate))
    lock_path = _env_lock_path(ep)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if not platform_compat.try_acquire_lock(lock_fd, exclusive=True):
            raise MigrationConflictError(
                f"{ep} is being written by another process; no rewrite "
                f"performed. Re-run `kirocrew secrets import --apply` to finish."
            )
        try:
            current_bytes = ep.read_bytes()
        except OSError:
            current_bytes = b""
        if current_bytes != original_bytes:
            raise MigrationConflictError(
                f"{ep} changed during migration (a concurrent write); no rewrite "
                f"performed. Re-run `kirocrew secrets import --apply` to finish "
                f"rewriting the .env references."
            )

        # CAS confirmed: .env is unchanged since the snapshot. NOW write the
        # secrets into the vault (still inside the lock), so a concurrent `.env`
        # edit that arrived between the snapshot and this point will have
        # triggered the CAS abort above, and the vault never holds a value
        # derived from a stale snapshot.
        #
        # `set_if_absent` (not `set`) closes the write-race with any OTHER vault
        # writer (dashboard secrets page, Weixin handler): the not-present check
        # is made INSIDE the vault's own cross-process store lock, so a
        # credential a concurrent writer saved between our earlier `list_names()`
        # snapshot and this write is never clobbered — that writer wins and we
        # abort rather than overwrite its newer value with the stale `.env`
        # plaintext, and rather than rewrite the `.env` line to point at a value
        # we did not store. The vault write API is async; a short-lived event
        # loop is fine inside the `try:` that holds the file lock.
        # Maps key -> the exact ENCRYPTED ENTRY dict THIS run stored via
        # set_if_absent. Used by the rollback to perform a ciphertext-identity
        # delete: only delete a vault entry if it still holds the exact
        # ciphertext we wrote, so a concurrent writer that overwrote our entry
        # between the store and the failure is never silently wiped (data loss).
        # Because _encrypt_entry uses a random nonce per call, two encryptions of
        # the same plaintext produce different dicts — the entry dict is a unique
        # fingerprint for what this run wrote.
        newly_written: dict[str, dict[str, str]] = {}

        async def _store_all() -> None:
            for key, value in to_store.items():
                entry = await vault.set_if_absent(key, value)
                if entry is None:
                    raise MigrationConflictError(
                        f"another writer stored {key!r} in the vault during the "
                        f"import; no rewrite performed so its value is not "
                        f"overwritten. Re-run `kirocrew secrets import --apply` "
                        f"to reconcile."
                    )
                newly_written[key] = entry

        # The store AND the re-verify/rewrite run inside ONE try whose except
        # rolls back every vault entry this run wrote. This is essential for a
        # PARTIAL store failure: a multi-key import that stores key 1 then hits a
        # conflict on key 2 raises from INSIDE _store_all — if that raise were
        # outside the rollback scope, key 1 would remain an orphaned vault entry
        # that vault-first resolution could later use to shadow a rotated
        # plaintext. `newly_written` is appended per successful write, so on any
        # failure it holds exactly the entries to undo.
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_store_all())
            finally:
                loop.close()

            # Re-verify the vault still matches what we are about to reference, then
            # perform the final CAS re-read and atomic .env rewrite — all inside a
            # single held vault cross-process lock so no concurrent vault DELETE can
            # sneak in between the verify and the write.
            #
            # ORDERING / DEADLOCK NOTE: _store_all() uses set_if_absent() which
            # acquires the vault's cross-process flock per-write and releases it
            # before returning.  hold_cross_process_lock() is therefore acquired
            # AFTER _store_all() has fully released all its per-write locks.  While
            # holding the vault lock here we only call vault.get() (read-only, no
            # flock acquired) and atomic_write (plain OS call), so there is NO nested
            # re-acquire and NO deadlock risk.
            #
            # The vault lock closes the race: without it a concurrent vault DELETE
            # could remove a key between the re-verify read (vault.get() returns the
            # expected value) and the atomic_write that commits `secret://KEY` to
            # .env — stranding the plaintext. Holding the lock for both operations
            # makes them an atomic unit from the vault's perspective: any DELETE that
            # arrives while we hold the lock is queued until after atomic_write
            # commits, at which point the `secret://KEY` reference in .env is already
            # durable and the DELETE only removes the vault entry (the credential is
            # gone, but not silently — that is the operator's explicit intent).
            with vault.hold_cross_process_lock():
                # `to_migrate` maps every key->value we intended to migrate (the value
                # from the original .env). For already_in_vault keys, the pre-lock
                # verification above already confirmed vault==.env, so `to_migrate[key]`
                # is the authoritative expected value for all keys in the set.
                # vault.get() is synchronous (no event loop needed) and does NOT
                # acquire the cross-process flock, so calling it under
                # hold_cross_process_lock() is safe.
                for _key, _expected in to_migrate.items():
                    try:
                        _entry = vault.get(_key)
                    except Exception as _exc:
                        raise MigrationConflictError(
                            f"vault entry for {_key!r} could not be read after "
                            f"storing ({_exc.__class__.__name__}); no .env rewrite "
                            f"performed so the plaintext is not lost — re-run "
                            f"`kirocrew secrets import --apply`."
                        ) from _exc
                    if _entry is None:
                        raise MigrationConflictError(
                            f"vault entry for {_key!r} was deleted during migration; "
                            f"no .env rewrite performed so the plaintext is not lost "
                            f"— re-run `kirocrew secrets import --apply`."
                        )
                    if _entry.reveal() != _expected:
                        raise MigrationConflictError(
                            f"vault entry for {_key!r} was changed during migration; "
                            f"no .env rewrite performed so the plaintext is not lost "
                            f"— re-run `kirocrew secrets import --apply`."
                        )

                # The .env was decoded with errors="surrogateescape", so new_text may
                # carry lone surrogates for any non-UTF-8 bytes in the original file.
                # atomic_write encodes a str as STRICT UTF-8 (which raises on those
                # surrogates), so hand it bytes re-encoded with surrogateescape — this
                # round-trips the original non-UTF-8 bytes faithfully and byte-for-byte.
                #
                # FINAL CAS re-read immediately before the write. The earlier CAS check
                # ran before the vault store + re-verify; between that point and this
                # write a writer that somehow bypassed the .env lock could have rewritten
                # the .env (e.g. a freshly rotated Weixin token written without the lock,
                # or a future writer added without adopting the lock convention).
                # `new_text` was built from `original_bytes`, so committing it now would
                # CLOBBER that writer's change.  Re-read the bytes one last time and
                # abort if they no longer match the snapshot: we never overwrite a .env
                # that moved since we composed the rewrite.  This shrinks the clobber
                # window to the atomic_write itself (an unavoidable last-writer race
                # shared by any file writer), rather than the whole store+verify span.
                try:
                    final_bytes = ep.read_bytes()
                except OSError:
                    final_bytes = b""
                if final_bytes != original_bytes:
                    raise MigrationConflictError(
                        f"{ep} changed during migration (a concurrent write) just "
                        f"before the rewrite; no rewrite performed so the concurrent "
                        f"change is not clobbered. Re-run `kirocrew secrets import "
                        f"--apply` to finish rewriting the .env references."
                    )
                atomic_write(
                    ep,
                    new_text.encode("utf-8", errors="surrogateescape"),
                    mode=0o600,
                    fsync=True,
                    restrict_to_owner=True,
                )
        except BaseException:
            # The .env rewrite failed (or never ran). Roll back every vault entry
            # THIS run wrote so a later re-run starts from the original plaintext
            # rather than finding an orphan vault entry that shadows a future
            # rotation. Best-effort: only delete keys this exact call stored via
            # set_if_absent (newly_written); pre-existing entries owned by
            # another writer are never touched. Deletion happens inside the held
            # .env file lock so no concurrent reader sees a half-committed state.
            # We use a fresh event loop because the failed `with` block may have
            # already closed or invalidated the previous one.
            #
            # CIPHERTEXT-IDENTITY DELETE: pass the exact entry dict returned by
            # set_if_absent to _compare_and_delete_sync. Because _encrypt_entry
            # uses a random nonce per call, a concurrent writer that stored any
            # new value (even the same plaintext) produces a different ciphertext
            # dict — the stored dict will differ from our dict, so their entry is
            # never touched.
            if newly_written:
                _rollback_loop = asyncio.new_event_loop()
                try:
                    _orphaned: list[str] = []

                    async def _rollback() -> None:
                        for _k, _written_entry in newly_written.items():
                            try:
                                # Ciphertext-identity delete under a single
                                # cross-process flock: compare the stored entry
                                # dict against the exact dict set_if_absent
                                # returned, and delete only on exact match.
                                # Because _encrypt_entry uses a random nonce,
                                # two encryptions of the same plaintext produce
                                # different dicts — a concurrent writer that
                                # stored a new value (even the same plaintext)
                                # has a different stored dict and is never
                                # deleted.  We use the sync
                                # _compare_and_delete_sync primitive rather than
                                # vault.delete() to avoid re-acquiring the flock
                                # (vault.delete -> _write_store -> _cross_process_lock
                                # -> hold_cross_process_lock would deadlock on POSIX
                                # if the caller held the lock externally).
                                await asyncio.to_thread(
                                    vault._compare_and_delete_sync, _k, _written_entry
                                )
                            except Exception as _rb_exc:
                                # Rollback-delete failed (e.g. ENOSPC, I/O error).
                                # The vault entry for _k may still exist and could
                                # shadow a future plaintext rotation.  Record the
                                # key so the caller can surface it to the operator.
                                _orphaned.append(_k)
                                logger.warning(
                                    "secrets import rollback: failed to delete vault"
                                    " entry for %r (%s: %s). The entry may persist"
                                    " and shadow future rotations — delete it under"
                                    " Settings > Secrets in the dashboard to clean"
                                    " it up.",
                                    _k,
                                    type(_rb_exc).__name__,
                                    _rb_exc,
                                )

                    _rollback_loop.run_until_complete(_rollback())
                    if _orphaned:
                        report.orphaned_vault_keys.extend(_orphaned)
                finally:
                    _rollback_loop.close()
            raise
        # Record what was migrated only on the success path — after the .env
        # rewrite commits — so an abort at any earlier guard leaves
        # report.migrated empty (nothing was permanently changed from the
        # caller's perspective).
        report.migrated.extend(to_migrate)
    finally:
        platform_compat.release_lock(lock_fd)
        os.close(lock_fd)
    return report


def format_report(report: MigrationReport) -> str:
    """Render *report* as human-readable CLI output."""
    lines: list[str] = []
    ep = report.env_file
    if report.dry_run:
        keys = report.migrated
        if not keys:
            lines.append(f"No plaintext credentials to migrate in {ep}.")
        else:
            lines.append(f"Would migrate {len(keys)} secret(s) from {ep} into the vault:")
            for k in keys:
                lines.append(f"  {k}  ->  secret://{k}")
            lines.append("")
            lines.append("Re-run with --apply to store these in the vault and rewrite")
            lines.append("each .env line to a secret:// reference.")
    else:
        keys = report.migrated
        if not keys:
            lines.append(f"Nothing migrated: no plaintext credentials found in {ep}.")
        else:
            lines.append(f"Migrated {len(keys)} secret(s) into the vault and rewrote {ep}:")
            for k in keys:
                lines.append(f"  {k}  ->  secret://{k}")
            lines.append("")
            lines.append("The original values were REWRITTEN in place to secret:// references;")
            lines.append("the plaintext is no longer present for these keys. Any values that")
            lines.append("Kiro Crew does not recognize as credentials were left untouched. If")
            lines.append(f"you keep a backup of {ep}, delete the plaintext copy once verified.")

    if report.already_referenced:
        lines.append("")
        lines.append("Already using secret:// (skipped): " + ", ".join(report.already_referenced))
    return "\n".join(lines)
