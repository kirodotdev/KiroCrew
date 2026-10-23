"""Operator-granted vault secrets for script crons, pinned to the code they were granted to.

A grant maps env-var names to vault secret names. It is validated when it is
requested, pinned by a keyed digest over the script body and the grant (with a
per-job epoch the operator's approval commits), and checked again before every run:
``_secret_env_precheck`` fails the run closed on a pin mismatch. At run time only the
granted names reach the child's environment, and their values are scrubbed from
everything the child prints.

Part of :mod:`kiro_crew.cron_script`, the import path callers, tests and docs use: it
reaches every name here, and a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`). Logs under the ``kiro_crew.cron_script`` logger.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.loader import config_dir
from kiro_crew.cron_script_child_env import _CRON_ENV_DENY
from kiro_crew.cron_script_paths import resolve_script_path
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.secrets import SecretVault

logger = logging.getLogger("kiro_crew.cron_script")


# ---------------------------------------------------------------------------
# Operator-granted vault secrets for SCRIPT crons (script jobs only).
#
# A grant maps an env-var NAME -> a vault secret NAME (kiro_crew.secrets
# SecretVault). It becomes ACTIVE only through the operator-approved request
# flow (agent requests, owner approves on the Schedule page); no surface
# lets an agent grant itself vault access. At fire time the secrets are
# resolved in-memory in the runner and delivered over the child's stdin
# (never the execve env), and never persisted in plaintext.
#
# The grant is pinned to the job's code: keyed HMAC over the script body (plus
# the script spec), computed when the operator grants — SCRIPT jobs only. Scripts
# under <config_dir>/crons/ are agent-writeable by design, so without the pin
# a granted job's body could be rewritten into an exfiltrator after approval.
# A pin mismatch fails the run closed — no injection, no fallback run — until
# the operator re-approves.
# ---------------------------------------------------------------------------

#: Grant env-var names: conventional uppercase env grammar only.
_SECRET_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: Grammar for the VALUE half of a grant map (the vault secret name). The
#: agent supplies it and the owner view + approval errors echo it, so it must
#: never be able to carry credential-shaped or URL-shaped content.
_SECRET_ENV_VAULT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Names a grant may never use, beyond the always-scrubbed _CRON_ENV_DENY:
#: process-behavior variables that would let an injected value alter HOW the
#: child runs (loader hijack, import shadowing, shell startup) rather than
#: merely being data the script reads.
_SECRET_ENV_DENIED_EXACT: frozenset[str] = (
    frozenset({"PATH", "HOME", "SHELL", "TMPDIR", "IFS", "ENV", "BASH_ENV"}) | _CRON_ENV_DENY
)

_SECRET_ENV_DENIED_PREFIXES: tuple[str, ...] = (
    "KIROCREW",  # product-internal, incl. _KIROCREW_* dial/secret plumbing
    "_KIROCREW",
    "LD_",  # ELF loader (LD_PRELOAD / LD_LIBRARY_PATH)
    "DYLD_",  # macOS loader
    "PYTHON",  # PYTHONPATH / PYTHONSTARTUP would shadow the launcher's imports
)

#: Cap mirrors the intent of the per-field caps in cron_service/fields.py: a grant is a small
#: hand-written map, not a bulk store.
_SECRET_ENV_MAX_ENTRIES = 16


def validate_secret_env_grant(secret_env: dict[str, str]) -> None:
    """Validate a secret grant map (env-var name -> vault secret name).

    Raises ValueError naming the offending KEY only — a vault secret name is
    operator data and may be echoed, but keep messages key-first for
    consistency with the resolver's no-echo discipline.
    """
    if len(secret_env) > _SECRET_ENV_MAX_ENTRIES:
        raise ValueError(
            f"secret_env holds {len(secret_env)} entries; max {_SECRET_ENV_MAX_ENTRIES}"
        )
    for key, name in secret_env.items():
        if not isinstance(key, str) or not _SECRET_ENV_NAME_RE.match(key):
            raise ValueError(
                f"secret_env key {key!r} is not a valid env-var name " "(expected [A-Z][A-Z0-9_]*)"
            )
        if key in _SECRET_ENV_DENIED_EXACT or any(
            key.startswith(p) for p in _SECRET_ENV_DENIED_PREFIXES
        ):
            raise ValueError(f"secret_env key {key!r} is a protected env-var name")
        if (
            not isinstance(name, str)
            or not name
            or name != name.strip()
            or not _SECRET_ENV_VAULT_NAME_RE.match(name)
        ):
            # Mirrors the vault storage boundary (names are stripped on store)
            # AND constrains the value to slug grammar: the grant map is
            # agent-supplied and its vault-name half is echoed to the owner
            # view and into approval error messages, so an unconstrained
            # string would be an unredacted channel for credential-shaped or
            # URL-shaped content. A name that cannot match can never resolve
            # to a sanely-named vault entry anyway.
            raise ValueError(
                f"secret_env entry {key!r} must reference a vault secret name "
                "matching [A-Za-z0-9][A-Za-z0-9._-]* (max 128 chars)"
            )


def _grant_pin_key() -> bytes:
    """HMAC key for grant code pins, derived from the agent-fenced vault key.

    A plain hash pin can be recomputed by anything that can write the cron
    store — on a host whose OS sandbox backend degrades to "none", a cron
    child is an ordinary subprocess and the store file is reachable — so the
    pin must be unforgeable, not merely collision-resistant. The key is a
    purpose-scoped derivation of the EXISTING vault key (a keystone leaf under
    ``<config_dir>/.vault/`` the agent's tools cannot read and sandboxed cron
    children never see), so there is no second key file, no separate birth
    race, and no separate corruption mode: the vault key's exclusive-create
    birth, fsync durability, and owner-only ACL are inherited. Raises
    ``ValueError`` when the vault store exists but its key is missing —
    grants fail closed rather than mint under a fresh key.
    """
    return SecretVault(config_dir()).derive_subkey("cron-grant-pin")


def _pin_digest(payload: bytes, *, domain: str, job_id: str, epoch: int | None = None) -> str:
    """Digest a pin payload according to its domain's trust model.

    ``pending`` pins are UNKEYED sha256: they authorize nothing (only the
    owner's approval mints the pin the runners honour) and they must be
    computable by the MCP server, whose sandbox hides the ``.vault`` dir —
    a keyed pending pin would make every agent request fail at mint time.
    Their job is integrity: approval recomputes the digest against the
    CURRENT code and refuses on drift, so what the operator blesses is what
    the agent showed them.

    ``active`` pins are HMAC-SHA256 under the vault-derived grant key AND
    include the job's grant EPOCH, so neither a forged store entry (no key)
    nor a replayed formerly-valid pin (dead epoch after any grant write)
    verifies at fire time.
    """
    if domain == "pending":
        return hashlib.sha256(payload).hexdigest()

    effective_epoch = _grant_epoch(job_id) if epoch is None else epoch
    epoch_payload = b"epoch\x00" + str(effective_epoch).encode() + b"\x00" + payload
    return hmac.new(_grant_pin_key(), epoch_payload, hashlib.sha256).hexdigest()


#: Size cap for a GRANTED script's body read (pin mint, approval re-verify,
#: fire-time verify all read the whole file). Two reasons, both load-bearing:
#: the file is agent-writeable, so an unbounded read_bytes() of a
#: multi-gigabyte sparse replacement would OOM the gateway — and the cap
#: MUST NOT exceed the dashboard's script-preview ceiling
#: (handlers/cron._SCRIPT_SOURCE_MAX_BYTES), because the approval signs the
#: whole body while the owner reviews only the preview: a grantable script
#: larger than the preview would let an agent hide executable tail bytes
#: behind the truncation and have the operator approve code they never saw.
_MAX_GRANTED_SCRIPT_BYTES = 256 * 1024


def _read_script_body(file_path: str) -> bytes:
    """Read a granted script's bytes through the guarded chokepoint.

    Routes through :func:`kiro_crew.hooks.safe_read_file_bytes_nolink`: the
    open is O_NOFOLLOW and inode-pinned (no hardlink/symlink swap between
    check and read), the opened descriptor must resolve inside the crons
    dir, and the read is bounded by ``_MAX_GRANTED_SCRIPT_BYTES``. Raises
    ``ValueError`` on any rejection — every grant path fails closed rather
    than minting or verifying a pin for bytes it could not safely read.
    """
    try:
        body = safe_read_file_bytes_nolink(
            file_path,
            str(config_dir() / "crons"),
            max_bytes=_MAX_GRANTED_SCRIPT_BYTES,
        )
    except FileTooLargeError as exc:
        raise ValueError(str(exc)) from exc
    if body is None:
        raise ValueError("script unreadable, hardlinked, or outside the crons dir — grant refused")
    return body


def _grant_epochs_path() -> Path:
    return config_dir() / ".vault" / ".grant_epochs.json"


def _read_grant_epochs(path: Path) -> dict[str, Any]:
    """The epochs map; a MISSING file is ``{}``, a corrupt file RAISES.

    The distinction is load-bearing: epochs only ever go up, so a counter
    that silently restarts at 0 would let a pin an agent saved under a low
    epoch verify again once new grants cycle the counter back through it —
    replaying a revoked grant. A file that exists but cannot be parsed
    therefore fails closed (``ValueError``) instead of reading as empty;
    only the genuinely-never-granted state (no file at all) reads 0.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"grant-epoch state unreadable: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(
            "grant-epoch state corrupt (invalid JSON); granted runs are "
            "refused until the owner restores or removes it"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("grant-epoch state corrupt (not an object)")
    return data


def _grant_epoch(job_id: str) -> int:
    """Current grant epoch for a job (0 when never granted/revoked).

    Epochs live under the agent-fenced ``.vault`` dir, OUTSIDE the
    agent-writable cron store: an agent that saved a formerly-valid
    mapping+pin and writes them back into the store after a revoke gets a
    pin minted under a dead epoch, and the runner refuses it. Only the
    gateway's own grant paths can bump an epoch. Raises ``ValueError`` on
    corrupt epoch state (see :func:`_read_grant_epochs`) — every caller
    (pin verification, mint, bump) then fails closed.
    """
    data = _read_grant_epochs(_grant_epochs_path())
    try:
        return int(data.get(job_id, 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("grant-epoch state corrupt (non-integer epoch)") from exc


def peek_grant_epoch(job_id: str) -> int:
    """The epoch the NEXT grant write will commit (current + 1), no write.

    Grant flows mint the new pin under this value BEFORE touching the store,
    and commit it (:func:`commit_grant_epoch`) only after the store swap
    succeeds — so a refusal or failure anywhere in between leaves the
    existing grant state untouched, and a crash between swap and commit
    leaves the NEW grant failing closed (re-approve fixes it) rather than
    ever leaving a dead pin on a grant the operation did not replace.
    """
    return _grant_epoch(job_id) + 1


def grant_epoch_ids() -> set[str]:
    """Job ids carrying a committed grant epoch entry.

    Job-removal paths use this to decide which deleted ids must bump: an id
    with an epoch entry once had an ACTIVE pin minted under it (only the
    approval path commits entries), so a saved copy of that record stays
    replayable until the entry is bumped — even when the agent-writable
    store has dropped its grant fields. Raises on corrupt epoch state
    (fail closed, same as :func:`bump_grant_epoch`).
    """
    path = _grant_epochs_path()
    with _grant_epochs_guard():
        return set(_read_grant_epochs(path))


#: Serializes grant-epoch read-modify-writes ACROSS THREADS in this process;
#: the cross-process half of the guarantee is the flock in
#: :func:`_grant_epochs_guard`. Both are needed: job-removal paths bump
#: epochs and removals also run from the CLI (``kirocrew cron remove``), so
#: the writer set is not one gateway process — a thread lock alone
#: would let a gateway revoke and a CLI removal read one epoch map and
#: overwrite each other's bump, reviving a revoked pin.
_GRANT_EPOCHS_LOCK = threading.Lock()


@contextmanager
def _grant_epochs_guard() -> Any:
    """Cross-process critical section for grant-epoch read-modify-writes.

    Takes the in-process thread lock, then an exclusive flock on a dedicated
    lockfile beside the epochs file (never the epochs file itself — it is
    atomically replaced, which would orphan a lock held on the old inode).
    A fresh descriptor per entry keeps the flock non-reentrant and simple;
    the thread lock in front prevents two threads of one process from
    blocking each other inside the flock wait.
    """
    lock_path = _grant_epochs_path().with_name(".grant_epochs.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _GRANT_EPOCHS_LOCK:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            platform_compat.acquire_lock(fd, exclusive=True)
            try:
                yield
            finally:
                platform_compat.release_lock(fd)
        finally:
            os.close(fd)


def _write_grant_epochs(path: Path, data: dict[str, Any]) -> None:
    """Durably replace the epochs file (fsync file, atomic rename, fsync dir).

    The fsyncs are part of the revoke fence, not politeness: a bump that a
    crash rolls back would revive every pin minted under the old epoch, so
    the write must be on disk — and the rename reachable — before the caller
    reports the revoke/grant as done. Directory fsync is best-effort where
    the platform has no ``O_DIRECTORY`` (Windows).
    """
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data))
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    dir_flag = getattr(os, "O_DIRECTORY", None)
    if dir_flag is None:
        return
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY | dir_flag)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def commit_grant_epoch(job_id: str, value: int, *, expected_current: int | None = None) -> bool:
    """Persist a job's grant epoch; returns ``False`` on a lost race.

    ``expected_current`` makes the commit compare-and-swap: pass the epoch
    :func:`peek_grant_epoch` was based on, and a stored value that has moved
    since (a concurrent revoke or job removal bumped it) REFUSES the commit
    instead of silently re-committing the very value that bump minted — the
    caller must then kill its own just-minted pin (bump once more) and
    surface the conflict. Without the check, an approval racing a removal
    could commit the removal's bumped epoch and leave the deleted grant
    record replayable. ``None`` skips the check (unconditional write).

    Corrupt epoch state raises (:func:`_read_grant_epochs`) rather than
    being replaced with a fresh map — overwriting it would reset every
    OTHER job's counter and revive their revoked pins.
    """
    path = _grant_epochs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _grant_epochs_guard():
        data = _read_grant_epochs(path)
        if expected_current is not None:
            try:
                current = int(data.get(job_id, 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("grant-epoch state corrupt (non-integer epoch)") from exc
            if current != expected_current:
                return False
        data[job_id] = int(value)
        _write_grant_epochs(path, data)
    return True


def bump_grant_epoch(job_id: str) -> int:
    """Advance a job's grant epoch; every active pin minted before it dies.

    REVOKE calls this BEFORE clearing the store: if the clear then fails,
    the store still names a grant but its pin is dead under the new epoch —
    the failure direction is fail-closed, never a still-live revoked secret.
    Grant/replace/promote flows use peek+commit instead (see
    :func:`peek_grant_epoch`) so a refused operation never touches the
    epoch of a grant it did not replace. The whole read-increment-write runs
    under the cross-process epochs guard so overlapping bumps — including
    one from a CLI removal in another process — cannot collapse into one.
    Corrupt epoch state raises instead of restarting the counter at 0 —
    under that state every granted run already refuses, so the failure
    direction stays closed (see :func:`_read_grant_epochs`).
    """
    path = _grant_epochs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _grant_epochs_guard():
        data = _read_grant_epochs(path)
        try:
            value = int(data.get(job_id, 0)) + 1
        except (TypeError, ValueError) as exc:
            raise ValueError("grant-epoch state corrupt (non-integer epoch)") from exc
        data[job_id] = value
        _write_grant_epochs(path, data)
    return value


def delivery_fingerprint(
    session_key: str = "",
    silent: bool = False,
    channel: str = "",
    thread_ts: str = "",
) -> str:
    """Canonical fingerprint of a job's DELIVERY controls, for pin binding.

    Covers every agent-mutable field that decides where (session_key,
    channel, thread_ts) or whether (silent) a run's output is delivered:
    binding them one by one invites the next omission, so the pin binds
    this single canonical blob instead. Rewiring ANY of them under a
    still-valid pin fails the run closed and asks for re-approval.
    """
    return json.dumps(
        {
            "channel": channel or "",
            "session_key": session_key or "",
            "silent": bool(silent),
            "thread_ts": thread_ts or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _pin_payload(
    script: str,
    command: str,
    message: str,
    body: bytes | None,
    job_id: str,
    grant: dict[str, str] | None,
    domain: str,
    delivery: str = "",
) -> bytes:
    """Canonical byte payload for a grant pin (see compute_secret_env_pin).

    ``delivery`` is the job's delivery-control fingerprint
    (:func:`delivery_fingerprint`) — which session and channel receive the
    run's output, and whether delivery happens at all. It is bound into the
    pin because the store is agent-writable: without it an agent could
    rewrite the delivery controls under a still-valid pin and steer or
    surface an approved script's granted output somewhere the operator
    never blessed.
    """
    canonical_grant = json.dumps(grant or {}, sort_keys=True, separators=(",", ":")).encode()
    head = (
        b"v4\x00"
        + domain.encode()
        + b"\x00"
        + job_id.encode()
        + b"\x00"
        + delivery.encode()
        + b"\x00"
        + canonical_grant
    )
    if script:
        assert body is not None
        return (
            head + b"\x00script\x00" + script.encode() + b"\x00" + message.encode() + b"\x00" + body
        )
    # Command jobs are refused BY DESIGN: a pin over the command TEXT cannot
    # cover the bytes of a helper file the command invokes (`bash helper.sh`
    # runs whatever the agent last wrote there under a still-valid pin), so a
    # command grant offers script-grade assurance in name only.
    raise ValueError("secret grants apply only to script jobs")


def compute_secret_env_pin(
    script: str,
    command: str,
    message: str = "",
    *,
    job_id: str = "",
    grant: dict[str, str] | None = None,
    domain: str = "active",
    epoch: int | None = None,
    body: bytes | None = None,
    delivery: str = "",
) -> str:
    """Pin a grant to the job's current code; returns a hex digest.

    ``pending``-domain pins are unkeyed SHA-256 (computable by the sandboxed
    MCP server, integrity-only); ``active``-domain pins are HMAC-SHA256
    under the vault-derived grant key with the grant epoch bound in — see
    :func:`_pin_digest` for the trust model of each domain.

    The pin binds, in one digest: the DOMAIN (``pending`` for an
    agent-requested grant awaiting approval, ``active`` for an operator-minted
    grant the runners honour — so a pending pin copied verbatim into the
    active fields never verifies), the JOB ID (a pin cannot be replayed onto
    another job), the DELIVERY FINGERPRINT (session_key/silent/channel/thread_ts — rewiring where or whether the
    run's output lands under a still-valid pin fails the run instead), the
    canonical GRANT MAPPING (swapping which secrets flow
    under an existing pin breaks it), and the job's code. Script jobs pin the
    script SPEC, the job ``message``, and the file's current bytes — the
    message is included because a script reads it as its ARGUMENTS
    (``ctx.message``: a channel, a URL, a query) and it is agent-updatable via
    ``cron_update``, so leaving it unpinned would let an approved script be
    re-aimed at an unapproved destination. Command jobs are refused (see
    :func:`_pin_payload`). The digest is
    HMAC-SHA256 under the vault-fenced grant key (see :func:`_grant_pin_key`),
    so a pin cannot be forged by editing the cron store — only the product's
    own grant paths can mint one. Raises
    (FileNotFoundError/PermissionError/ValueError) when the script spec does
    not resolve — a grant must never be minted for code that cannot be read.

    ``body`` lets a caller pin SPECIFIC script bytes it already read: two pins
    derived for one decision (verify the pending, mint the active) MUST come
    from one snapshot, or an agent swapping the file between two reads would
    get code the approver never saw blessed with the active pin.
    """
    if script:
        if body is None:
            file_path, _func = resolve_script_path(script)
            body = _read_script_body(file_path)
    return _pin_digest(
        _pin_payload(script, command, message, body, job_id, grant, domain, delivery),
        domain=domain,
        job_id=job_id,
        epoch=epoch,
    )


def _resolve_secret_env(secret_env: dict[str, str]) -> dict[str, str]:
    """Resolve a validated grant map to plaintext values from the vault.

    Fail-closed: a missing vault entry raises ValueError naming the env-var
    KEY (never echoing the secret name — same CWE-117 discipline as
    mcp_gateway.secret_uri). Runs in the cron pool worker thread, so the
    blocking vault file read is off the event loop.
    """
    vault = SecretVault(config_dir())
    fetched = vault.get_many(list(secret_env.values()))
    resolved: dict[str, str] = {}
    for key, name in secret_env.items():
        value = fetched.get(name)
        if value is None:
            raise ValueError(
                f"cron secret grant for env var {key!r} references a vault "
                "secret that does not exist. Store it under Settings > Secrets "
                "in the dashboard, or update the grant."
            )
        resolved[key] = value.reveal()
    return resolved


def _filter_grant_env(resolved: dict[str, str]) -> dict[str, str]:
    """Drop protected keys from a resolved grant (defense in depth).

    Grants are validated at persistence; this re-check only guards a store
    edited outside the product. It runs on the delivery path — the script
    runner's stdin payload — so a
    grant can never name a product-internal key (``_KIROCREW*``) or a loader
    variable regardless of how it reaches the child. The skip log carries a
    COUNT only: the env-var names here flow from the same mapping as the
    secret values, so logging one keeps tripping taint scanners, and the
    operator can read the offending names from their own grant table in the
    dashboard.
    """
    kept: dict[str, str] = {}
    skipped = 0
    for key, value in resolved.items():
        if (
            not _SECRET_ENV_NAME_RE.match(key)
            or key in _SECRET_ENV_DENIED_EXACT
            or any(key.startswith(p) for p in _SECRET_ENV_DENIED_PREFIXES)
        ):
            skipped += 1
            continue
        kept[key] = value
    if skipped:
        logger.warning("cron secret grant: skipped %d protected env key(s)", skipped)
    return kept


def _scrub_grant_values(text: str, resolved: dict[str, str]) -> str:
    """Replace every granted secret VALUE occurring in ``text`` with a marker.

    Pattern-based ``redact`` only recognises known credential shapes; a vault
    value has no required shape, so a script exception that embeds one (an
    HTTP client echoing its auth header, or ``raise Exception(token)``) would
    pass through untouched. The parent resolved the exact values to build the
    child's stdin payload, so it can scrub them precisely from any diagnostic
    it is about to return — the launcher's own status JSON included. Longest
    values first, so a value that contains another as a substring cannot leave
    a recognisable fragment behind.
    """
    if not text or not resolved:
        return text
    for value in sorted(resolved.values(), key=len, reverse=True):
        # Every nonempty value is scrubbed: a short secret is still a secret,
        # and a mangled diagnostic beats a leaked credential. The vault
        # refuses empty values, so the guard below is belt-and-braces.
        if value and value in text:
            text = text.replace(value, "[redacted-grant-value]")
    return text


def _secret_env_precheck(
    secret_env: dict[str, str] | None,
    secret_env_pin: str,
    script: str = "",
    command: str = "",
    script_body: bytes | None = None,
    message: str = "",
    job_id: str = "",
    delivery: str = "",
) -> tuple[dict[str, str], str | None]:
    """Verify the grant pin and resolve secrets; ``(resolved, error)``.

    ``script_body`` carries the bytes the caller already read (and will
    execute) so the pin covers exactly what runs — never a second read of a
    file an agent could swap between check and use. The pin is verified in the
    ``active`` domain with this job's id, delivery fingerprint, and mapping bound in
    (see :func:`compute_secret_env_pin`), so a pending pin, another job's pin,
    a rewired delivery session, or
    the same pin over a different mapping all fail closed.
    """
    if not secret_env:
        return {}, None
    if not secret_env_pin:
        return {}, "secret grant has no code pin; re-approve it in the dashboard"

    try:
        current = _pin_digest(
            _pin_payload(
                script, command, message, script_body, job_id, secret_env, "active", delivery
            ),
            domain="active",
            job_id=job_id,
        )
    except ValueError as exc:  # vault store without key — never inject
        return {}, str(exc)
    if not hmac.compare_digest(current, secret_env_pin):
        return {}, (
            "cron code changed (or its delivery session was rewired) since "
            "its secret grant was approved; secrets were NOT injected. "
            "Re-approve the grant in the dashboard (Schedule > job > "
            "Secrets) to run it again."
        )
    try:
        return _resolve_secret_env(secret_env), None
    except ValueError as exc:
        return {}, str(exc)
