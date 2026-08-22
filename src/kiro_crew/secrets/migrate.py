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
lock serializes against any OTHER PROCESS operating under the same lock (a second
importer, or any writer that adopts it), and — under that lock — a compare-and-swap
re-read aborts the rewrite (no write) if the bytes changed since the snapshot,
which additionally catches an in-process writer that does not take the file lock
(today the WeChat token handler, which serializes on an in-process
``asyncio.Lock``). If the lock cannot be taken, the migration aborts cleanly and a
re-run reconciles (idempotent, secret://-line-wins).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import _JIRA_TOKEN_RE, CRED_JIRA_API_TOKEN, config_dir, env_path
from kiro_crew.secrets import SecretVault

#: ``secret://`` scheme prefix a migrated ``.env`` value is rewritten to.
_SECRET_URI_PREFIX = "secret://"


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
    whether this pass wrote anything.
    """

    env_file: Path
    dry_run: bool
    migrated: list[str] = field(default_factory=list)
    already_referenced: list[str] = field(default_factory=list)


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
    ``KEY=secret://KEY``; everything else is emitted verbatim.
    """
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in migrated_keys:
                out.append(f"{key}={_SECRET_URI_PREFIX}{key}")
                continue
        out.append(line)
    # Preserve a trailing newline if the original had one.
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + trailing


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
                f"match the vault, or `kirocrew secrets set {key}`), then re-run "
                f"`kirocrew secrets import --apply`."
            )
        already_in_vault.add(key)
    to_store = {k: v for k, v in to_migrate.items() if k not in already_in_vault}

    # Apply: write every not-yet-stored secret into the vault BEFORE touching
    # .env, so a partial failure never leaves a secret:// reference pointing at
    # a vault entry that was never written. The vault's write API is async;
    # migration runs from the synchronous CLI (no running loop), so the
    # coroutine is driven on a dedicated event loop that is closed afterwards.
    #
    # `set_if_absent` (not `set`) closes the write-race with any OTHER vault
    # writer (dashboard secrets page, Weixin handler): the not-present check is
    # made INSIDE the vault's own cross-process store lock, so a credential a
    # concurrent writer saved between our earlier `list_names()` snapshot and
    # this write is never clobbered — that writer wins and we abort rather than
    # overwrite its newer value with the stale `.env` plaintext, and rather than
    # rewrite the `.env` line to point at a value we did not store.
    async def _store_all() -> None:
        for key, value in to_store.items():
            wrote = await vault.set_if_absent(key, value)
            if not wrote:
                raise MigrationConflictError(
                    f"another writer stored {key!r} in the vault during the "
                    f"import; no rewrite performed so its value is not "
                    f"overwritten. Re-run `kirocrew secrets import --apply` to "
                    f"reconcile."
                )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_store_all())
    finally:
        loop.close()
    report.migrated.extend(to_migrate)

    # Serialize the read-snapshot → compare → rewrite as one critical section
    # with an exclusive OS advisory lock (the mandated cross-platform helper —
    # POSIX flock / Windows msvcrt), taken on a sidecar lock file so it does not
    # perturb the .env's own bytes/mode. Two guards, each covering what the other
    # cannot:
    #   * the LOCK serializes against any OTHER PROCESS touching the .env under
    #     the same lock (a second `secrets import`, or any writer that adopts the
    #     lock), closing the cross-process compare-then-replace window;
    #   * the CAS re-read (below, under the lock) still catches an in-process
    #     writer that does NOT take this file lock — today the only in-tree one
    #     is the WeChat/Weixin handler, which serializes on an in-process
    #     asyncio.Lock — by aborting if the bytes changed since the snapshot.
    # If the lock cannot be taken (another holder), abort cleanly rather than
    # block the CLI; a re-run reconciles (idempotent, secret://-line-wins).
    new_text = _rewrite_env_text(text, set(to_migrate))
    lock_path = ep.with_name(ep.name + ".lock")
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
                f"performed. The vault was updated; re-run `kirocrew secrets "
                f"import --apply` to finish rewriting the .env references."
            )

        # The .env was decoded with errors="surrogateescape", so new_text may
        # carry lone surrogates for any non-UTF-8 bytes in the original file.
        # atomic_write encodes a str as STRICT UTF-8 (which raises on those
        # surrogates), so hand it bytes re-encoded with surrogateescape — this
        # round-trips the original non-UTF-8 bytes faithfully and byte-for-byte.
        atomic_write(
            ep,
            new_text.encode("utf-8", errors="surrogateescape"),
            mode=0o600,
            fsync=True,
            restrict_to_owner=True,
        )
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
